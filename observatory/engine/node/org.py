"""This machine's organization enrollments: `fabric-device/0.1`, `fabric-activity/0.1` and `share-registration/1`
(contract DEC-0031, DEC-0056, DEC-0082, DEC-0086; engine OBS-161, the producer half of OE-05 and OE-01b).

One enrollment per organization (DEC-0082), each in its own owner-only folder `<node>/orgs/<org.id>/`:

- `enrollment.json`: the server's origin, the certificate (serial, window, binding), the stream epoch, the protocols the
  server accepts, the check-in interval and `collectionStart`, and the device's check-in sequence. No secret: the
  enrollment token is read from the vault for the one request that needs it and is never stored, logged or printed.
- `outbox.sqlite`: the activity stream of this enrollment (`collector.id` `observatory-node`, the epoch the server
  issued), every event persisted once with its id and seq before it is sent, sent in contiguous slices of at most 1000,
  each batch signed afresh, retried with backoff under the same `batchId`, consumed by the server's ack; and the share
  registrations with the server's latest answer to each.
- `journal.jsonl`: structured, secret-free lines of what was done (bounded).

Each enrollment has device keys of its own (devices.md), in `<node>/orgs/<org.id>/keys/` (`node/device_keys.py`): an
owner-only PKCS#8 file, declared as such (`storage: file`, `exportable: true`, CT-65); the certificate request is
signed by its `#sig` key, and every request after enrollment carries its `fabric-device` HTTP signature, `keyid` the
certificate's serial (`http_signature.py`). A hardware key store (the Keychain helper, DEC-0053) plugs in through
`Enrollment(keys=...)` with its own `storage`. Every command that changes an enrollment holds its lock
(`<node>/orgs/.locks/<org.id>.lock`) and reads the state again under it. A share registration is derived only from the estate revision and the revocation-delta entries
this machine verified and applied (`records/`), and only toward a server whose enrollment response lists
`share-registration/1`; `grant.usage` for a grant is sent only when the server's latest answer to that grant's
registration lets it (`share_registration.gate`).

A certificate that ended while the machine was offline is renewed by the key on record (contract DEC-0100, OBS-199):
within 30 days of its `notAfter`, on the server's clock, `org check-in` and `org tick` send an enrollment request with
`auth.method: "key-on-record"`, no token and no `Authorization` header (the CSR's own signature by the enrollment's
`#sig` key is the proof), under the enrollment's lock, and take the new certificate and epoch as any re-enrollment.
A renewal the server refuses (a server pinned before DEC-0100 refuses it at its schema; a server may refuse it by
policy) is not asked again for that certificate: from there, as past the 30 days, the machine needs a new token.
"""
from __future__ import annotations

import base64
import contextlib
import datetime as _dt
import hashlib
import json
import logging
import os
import random
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import osprivacy
import private_io

from . import http_signature, node_summary, share_registration
from .relay import strict_time

LOG = logging.getLogger('observatory.node.org')

DEVICE_PROTOCOL = 'fabric-device/0.1'
ACTIVITY_PROTOCOL = 'fabric-activity/0.1'
SHARE_PROTOCOL = share_registration.TYPE
SUMMARY_PROTOCOL = node_summary.TYPE
#: What this node's check-in lists in its own `protocols`: what it READS from the server (DEC-0067, DEC-0094). A server
#: gates what it sends on this list (DEC-0116, DEC-0117), so a revision belongs here only once the node reads it:
#: `repair-request/1` with the repair runner (OBS-193); `credential-decision/1`, `fabric-credential-envelope/1`,
#: `credential-revocation/1` and `call-record/1` with the credential exchange (OBS-191, OBS-194). Until then no server
#: sends this node `records.repairs`, credential members or `callRecordsAccepted`.
NODE_READS = (DEVICE_PROTOCOL,)
#: DEC-0111's check-in members this node never sends yet (the credential exchange is OBS-191 and OBS-194): each would
#: go only to a server whose response lists `credential-request/1` or `call-record/1` (DEC-0116).
UNSENT_RECORDS = ('credentialRequests', 'callRecords')
#: A summary is sent when what it says changed, and at least this often (DEC-0101: "when it changed and at least once a
#: day").
SUMMARY_EVERY_SECONDS = 86400
#: How long a summary the server refused waits before it is tried again; the check-ins go on without it meanwhile.
SUMMARY_BACKOFF_SECONDS = 86400
#: The oldest scan a summary is built from: past it the scan no longer says what this machine runs (the tick takes
#: one every 30 minutes, so two days without one means it stopped).
SCAN_MAX_AGE_SECONDS = 48 * 3600
#: Answers to a check-in that carried a summary which may be the summary's fault: a malformed or too large request
#: whatever its code, or another refusal that names the summary or the records.
SUMMARY_SUSPECT_STATUSES = frozenset({400, 413, 422})
COLLECTOR_ID = 'observatory-node'
ORG_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
TOKEN = re.compile(r'([A-Za-z0-9][A-Za-z0-9._:~-]{0,127})\.([A-Za-z0-9_-]{16,256})\Z')
#: The receiver names its paths; the contract names none. These are the first organization server's, overridable.
PATHS = {'enroll': '/v1/devices/enroll', 'check-in': '/v1/devices/check-in', 'rotate': '/v1/devices/rotate',
         'batches': '/v1/activity/batches', 'share-registration': '/v1/devices/share-registrations'}
MAX_BATCH = 1000
#: The outbox's bound: past it the oldest pending events are dropped and one `buffer.overflow` names them.
MAX_PENDING = 10000
#: Dropping happens in steps, so a burst past the bound ends in one `buffer.overflow`, not one per event.
OVERFLOW_HEADROOM = 1000
BACKOFF_FLOOR, BACKOFF_CEILING = 30, 3600
#: A batch the receiver refuses for what it is (malformed, too large, another device's) is never sent again as it is:
#: retrying it would hold the stream forever. Every other failure (409 `activation-closed`, 429, 5xx, no answer) is
#: retried with backoff under the same `batchId`.
PERMANENT_BATCH_STATUSES = frozenset({400, 413, 422})
PERMANENT_BATCH_CODES = frozenset({'device-mismatch', 'payload-too-large', 'batch-invalid', 'collector-mismatch'})
#: An enrollment refused because the server's policy does not take this key's storage (CT-65: a server MAY refuse a
#: file-held key by policy): a named state, not a retry.
STORAGE_REFUSALS = frozenset({'key-storage-refused', 'key-storage-not-allowed', 'storage-not-allowed',
                              'attestation-required', 'key-not-hardware'})
#: The refusals that mean this machine is no longer in the organization (OBS-204), each only with HTTP 401 as the
#: contract sends them (devices.md, the step table): its certificate was revoked (an administrator removed the machine,
#: or a server pinned before DEC-0103 revoked every machine of a closing organization), or the server no longer knows it
#: and no previous certificate is left to fall back to (CT-65; the fallback runs first). An organization that ended
#: answers `ORGANIZATION_CLOSED` instead (DEC-0103, below) to a node pinned at or after it, so this node tells the
#: two apart when the server does. The enrollment ends there: nothing more is sent, its buffered events are dropped with
#: one count recorded, and only `org leave` or a new token's `org enroll` follow. A 5xx is never terminal: it is
#: retried as any other failure.
REMOVED_CODES = frozenset({'certificate-revoked', 'certificate-unknown'})
#: DEC-0103: the final refusal of an organization that ended, HTTP 410 with the body `device-organization-closed`
#: (`{error: {code, message}, closedAt}`), on any device request. Final on one answer: every request to that
#: organization stops, what was buffered for it (events, sums, registrations) is dropped, and the enrollment ends,
#: discarding that organization's server policy layer, key set, collection start and stream. A 410 whose body is not
#: that shape is no such answer: it is retried as any other failure, because ending an enrollment cannot be undone.
ORGANIZATION_CLOSED = 'organization-closed'
#: `unknown-org` is the first organization server's own enrollment refusal (DEC-0103: not a contract code): an
#: enrollment it refuses so names the organization id the server does not know. On a signed device request it means
#: nothing more than any other failure, and is retried as one.
UNKNOWN_ORG = 'unknown-org'
#: `activity-common.schema.json#/$defs/opaqueId`: a certificate's `binding.user.id` (no spaces, no @).
OPAQUE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}')
#: `device-common.schema.json#/$defs/detail`: one line of at most 200 characters with no path in it.
DETAIL = re.compile(r"""(?![\s\S]*(?:(?:^|[\s"'`=(\[{<,;:])[/\\~]|[A-Za-z]:[/\\]|%(?:25)?2[fF]"""
                    r"""|(?:^|[^A-Za-z0-9])(?:[Uu]sers|home)[/\\]|-(?:[Uu]sers|home)-[A-Za-z0-9]))[^\r\n]{1,200}\Z""")
#: `activity-common.schema.json#/$defs/noticeVersion`: the version of the organization's notice (DEC-0052).
NOTICE_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
#: DEC-0100: the receiver does not collect this event for this person under the notice they acknowledged. The collector
#: sends no event of that kind until the notice moves (DEC-0102, `purpose_held`), never on a timer.
PURPOSE = 'purpose'
#: The certificate refusals that end the enrollment: the machine needs a new token. `REMOVED_CODES` end it only with
#: HTTP 401 (`_ends`).
ENDING_CODES = frozenset({'epoch-superseded', 'certificate-expired'}) | REMOVED_CODES


def _ends(status: int, code: Optional[str]) -> bool:
    return code in ('epoch-superseded', 'certificate-expired') or (status == 401 and code in REMOVED_CODES)
#: `org status`, Health and doctor warn this long before the certificate expires (OBS-199).
EXPIRY_WARNING_SECONDS = 3 * 86400
#: How long after its certificate ended a machine renews by its key on record, without a token (contract DEC-0100,
#: `RENEWAL_GRACE_DAYS`): judged on the server's clock, the end included. Past it a new enrollment token is needed.
RENEWAL_GRACE_SECONDS = 30 * 86400
#: A renewal answered with one of these is retried with backoff: the server did not decide. Any other 4xx refuses it.
RENEWAL_RETRY_STATUSES = frozenset({0, 408, 425, 429})
TOKEN_SLOT_PLACEHOLDER = 'PROJECT/ENV/NAME'
JOURNAL_BYTES = 256 * 1024
_CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
_BOOT = 'boot-' + secrets.token_hex(4)

Transport = Callable[[str, Dict[str, str], bytes, float], Tuple[int, bytes]]


class OrgError(RuntimeError):
    """A refusal with a code: `{'error': code, 'message': ...}` for the terminal, never a secret. A refusal a person
    resolves carries the step in words (`next_step`) and the command it ends with (`command`)."""

    def __init__(self, code: str, message: str, *, next_step: Optional[str] = None, command: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.next_step, self.command = next_step, command


def enroll_command(org_id: str, server: Optional[str]) -> str:
    return 'project-observatory full org enroll --server %s --org %s --token-slot %s' % (
        server or 'https://ORG-SERVER', org_id, TOKEN_SLOT_PLACEHOLDER)


def renewal(state: dict, now: float) -> Optional[dict]:
    """Whether this enrollment renews by its key on record (contract DEC-0100), from its saved state alone: a
    certificate that ended (on the server's clock, `skewSeconds`, or as the server answered `certificate-expired`) at
    most `RENEWAL_GRACE_SECONDS` ago, of a machine not removed from its organization, that the server did not ask to
    enroll again for another reason, and whose renewal the server did not refuse for that certificate. None otherwise.
    `now` is this machine's clock. The answer is the window's end (`until`) and the retry state of a renewal that went
    unanswered (`attempts`, `nextAt`, `last`)."""
    if isinstance(state.get('removed'), dict):
        return None
    cert = state.get('certificate') if isinstance(state.get('certificate'), dict) else {}
    ends = strict_time(str(cert.get('notAfter') or ''))
    if ends is None:
        return None
    ends /= 1000
    server_now = now + (state.get('skewSeconds') or 0)
    said_expired = state.get('reEnrollReason') == 'certificate-expired'
    if state.get('reEnroll') and not said_expired:          # `re-enroll`, a superseded epoch: a token, not a renewal
        return None
    if server_now < ends and not said_expired:
        return None
    if server_now - ends > RENEWAL_GRACE_SECONDS:
        return None
    held = state.get('renewal') if isinstance(state.get('renewal'), dict) else {}
    if held.get('serial') != cert.get('serial'):
        held = {}
    if held.get('refused'):
        return None
    return {'by': 'key-on-record', 'until': iso(ends + RENEWAL_GRACE_SECONDS), 'attempts': held.get('attempts', 0),
            'nextAt': held.get('nextAt', 0), 'last': held.get('last')}


def notice(org_id: str, state: dict, now: float) -> dict:
    """What a person must know about one enrollment, from its saved state alone (no network, no key): the
    certificate's expiry date, its state (`valid`, `expiring` in the last three days, `expired`, `needs-enrollment`
    when the server asked for it, `removed` when the organization no longer has this machine), and, when the person
    has something to do, the step in words and the command. An expired certificate still inside its renewal window
    (DEC-0100) carries `renewUntil` and the check-in that renews it; a renewal the server refused, `renewalRefused`.
    `org status`, Health and doctor all read this.

    `now` is this machine's clock; the expiry is judged on the server's, as the last check-in measured it
    (`skewSeconds`), the clock every request is signed on."""
    now = now + (state.get('skewSeconds') or 0)
    server = state.get('server') if isinstance(state.get('server'), str) else None
    not_after_text = str((state.get('certificate') or {}).get('notAfter') or '')
    not_after_ms = strict_time(not_after_text)
    left = (not_after_ms / 1000 - now) if not_after_ms is not None else 0.0
    out: Dict[str, Any] = {'org': org_id, 'server': server, 'notAfter': not_after_text,
                           'expiresOn': not_after_text[:10], 'daysLeft': round(left / 86400, 2)}
    enroll = enroll_command(org_id, server)
    new_token = ("ask the organization's administrator for a new enrollment token, put it in a vault slot "
                 '(python "$(project-observatory full-path)/tools/vault.py" put PROJECT ENV NAME, the token on stdin) '
                 'and run: ' + enroll)
    removed = state.get('removed')
    renews = renewal(state, now - (state.get('skewSeconds') or 0))
    held = state.get('renewal') if isinstance(state.get('renewal'), dict) else {}
    refused = held.get('last') if held.get('refused') and held.get('serial') == \
        (state.get('certificate') or {}).get('serial') else None
    if isinstance(refused, dict):
        out['renewalRefused'] = refused
    if isinstance(removed, dict) and removed.get('code') == ORGANIZATION_CLOSED:
        out.update(state='removed', removed=removed, command='project-observatory full org leave --org %s' % org_id,
                   enrollCommand=enroll,
                   nextStep='organization %s closed on %s (the server answered %s on %s): nothing more is sent to it, '
                            'and what this machine held for it was dropped, its policy and keys with it. Remove its '
                            'local state with: project-observatory full org leave --org %s'
                            % (org_id, str(removed.get('closedAt') or '?')[:10], ORGANIZATION_CLOSED,
                               removed.get('at') or '?', org_id))
    elif isinstance(removed, dict):
        out.update(state='removed', removed=removed, command='project-observatory full org leave --org %s' % org_id,
                   enrollCommand=enroll,
                   nextStep='this machine was removed from organization %s on %s (the server answered %s): nothing '
                            'more is sent to it and what it held was dropped. Remove its local state with: '
                            'project-observatory full org leave --org %s; to join again, %s'
                            % (org_id, removed.get('at') or '?', removed.get('code') or '?', org_id, new_token))
    elif renews is not None:
        check_in = 'project-observatory full org check-in --org %s' % org_id
        out.update(state='expired', renewUntil=renews['until'], command=check_in,
                   nextStep='the certificate expired on %s. This machine renews it by itself with its key on record, '
                            'no token needed, the next time it reaches the organization server, until %s (the '
                            'maintenance job tries; now: %s). After %s it needs a new enrollment token'
                            % (not_after_text, renews['until'], check_in, renews['until']))
    elif left <= 0 or state.get('reEnrollReason') == 'certificate-expired':
        why = (' and the server refused its renewal by the key on record (%s, HTTP %s)'
               % (refused.get('code'), refused.get('status')) if isinstance(refused, dict) else
               ' more than 30 days ago' if left <= -RENEWAL_GRACE_SECONDS else '')
        out.update(state='expired', command=enroll,
                   nextStep='the certificate expired on %s%s, so the organization accepts nothing from this machine; '
                            'it needs a new enrollment token: %s' % (not_after_text, why, new_token))
    elif state.get('reEnroll'):
        out.update(state='needs-enrollment', command=enroll,
                   nextStep='the organization asked this machine to enroll again: ' + new_token)
    elif left <= EXPIRY_WARNING_SECONDS:
        out.update(state='expiring', command='project-observatory full org rotate --org %s' % org_id,
                   nextStep='the certificate expires on %s. The maintenance job renews it while the server answers; '
                            'renew it now with: project-observatory full org rotate --org %s. If this machine stays '
                            'offline past %s, an organization server that takes it renews it by its key on record '
                            'for 30 days after that; past them it needs a new enrollment token'
                            % (not_after_text, org_id, not_after_text))
    else:
        out['state'] = 'valid'
    out['summary'] = summary_notice(state)
    holds = Enrollment.purpose_holds(state)
    if holds and not isinstance(removed, dict):        # DEC-0100: what this machine does not send, and why
        out['purposeHolds'] = [{'kind': kind, 'since': hold.get('at'),
                                'waitsFor': {'acknowledgedNoticeVersion': hold.get('acknowledged'),
                                             'noticeVersion': hold.get('noticeVersion')}}
                               for kind, hold in sorted(holds.items())]
    return out


#: The largest scan file a summary is built from; past it the scan is reported unreadable rather than read whole.
SCAN_LIMIT = 32 * 1024 * 1024
#: The person's list of names never to send, owner-only, in the workspace's `config/` (`{"exclude": [glob, ...]}`).
EXCLUSIONS_FILE = 'node-summary.json'
EXCLUSIONS_MAX = 1000


def read_harness_scans(now: float, scratch: Optional[Path] = None,
                       enabled: Optional[Callable[[str], bool]] = None) -> Tuple[Any, Any, Optional[str]]:
    """The two scans a node summary is built from (`store/raw/harness.json` from `collectors/scan_harness.py`,
    `store/raw/mcp.json` from `collectors/scan_mcp.py`), and why it cannot be built when it cannot. Both are opt-in,
    and a summary built without one would tell the organization that this machine runs nothing of that kind, so each
    is refused when its integration is off (a scan left from before is not sent), when it is older than
    `SCAN_MAX_AGE_SECONDS` by its own time, when it cannot be read, and, for the harness scan, when it predates the
    node summary (no `plugins` or `coding_agents`)."""
    if scratch is None or enabled is None:
        import configuration as _configuration
        import paths as _paths
        scratch = Path(_paths.SCRATCH) if scratch is None else scratch
        enabled = _configuration.enabled if enabled is None else enabled
    docs = []
    for name, integration, stamp in (('harness', 'harness', 'generated_at'), ('mcp', 'mcp', 'scanned_at')):
        if not enabled(integration):
            return None, None, ('integrations.%s is off: the %s scan is not taken, and one left from before is not '
                                'sent' % (integration, name))
        try:
            doc = json.loads(osprivacy.read_regular(Path(scratch) / (name + '.json'), SCAN_LIMIT))
        except FileNotFoundError:
            return None, None, 'no %s scan on this machine yet (the tick has not run it)' % name
        except (OSError, ValueError) as error:
            return None, None, 'the %s scan is unreadable (%s)' % (name, type(error).__name__)
        if not isinstance(doc, dict):
            return None, None, 'the %s scan is not an object' % name
        taken = strict_time(doc.get(stamp))
        if taken is None:
            return None, None, 'the %s scan carries no time, so it cannot be called current' % name
        age = now - taken / 1000
        if age > SCAN_MAX_AGE_SECONDS:
            return None, None, ('the %s scan was taken %s, %d h ago, past the %d h a summary is built from'
                                % (name, doc.get(stamp), age // 3600, SCAN_MAX_AGE_SECONDS // 3600))
        if name == 'harness' and not all(isinstance(doc.get(k), list) for k in ('plugins', 'coding_agents')):
            return None, None, ('the harness scan predates the node summary (no plugins or coding agents listed); '
                                'the next tick takes a new one')
        docs.append(doc)
    return docs[0], docs[1], None


def read_exclusions(path: Optional[Path] = None) -> Tuple[List[str], Optional[str]]:
    """The person's exclusion list (`config/node-summary.json`, `{"exclude": [glob, ...]}`): shell-style globs matched
    against each whole name, applied before anything is checked or sent. Absent: none. The file itself must be a
    regular file, not a link, readable and writable by its owner only, in a folder nobody else may write to (the
    folder's own parents are resolved, so a workspace under a linked folder such as `/tmp` is fine). Anything else is
    a reason not to send at all, worded by what failed, never an empty list."""
    if path is None:
        import paths as _paths
        path = Path(_paths.CONFIG) / EXCLUSIONS_FILE
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return [], None
    where = 'the exclusion list config/%s' % EXCLUSIONS_FILE
    if path.is_symlink():
        return [], '%s is a symbolic link; make it a regular file' % where
    folder = path.parent.resolve()
    target = folder / path.name
    try:
        if osprivacy.others_may_write(folder):
            return [], '%s is in a folder others may write to; make the folder writable by its owner only' % where
        fd = osprivacy.open(target, os.O_RDONLY | osprivacy.NOFOLLOW | osprivacy.NONBLOCK)
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return [], '%s is not a regular file' % where
            if not osprivacy.private(stream.fileno()):
                return [], ('%s is readable by others; make it owner-only (chmod 600 on POSIX)' % where)
            if info.st_size > 1024 * 1024:
                return [], '%s is larger than 1 MB' % where
            doc = json.loads(stream.read())
    except (OSError, ValueError) as error:
        return [], '%s is unreadable (%s)' % (where, type(error).__name__)
    globs = doc.get('exclude') if isinstance(doc, dict) else None
    if not isinstance(globs, list) or len(globs) > EXCLUSIONS_MAX or not all(isinstance(g, str) and g for g in globs):
        return [], ('%s must be {"exclude": [glob, ...]}, at most %d non-empty strings' % (where, EXCLUSIONS_MAX))
    return list(globs), None


def _summary_carried(previous: Optional[dict], renewal: bool) -> dict:
    """What of the harness summary outlives a renewal or a re-enrollment, which write the state afresh (OBS-192):
    the sequence always (DEC-0101: it rises with every summary of this device and context, and a server may refuse one
    that goes back). Only a renewal by the key on record (the same enrollment, the same person, `binding-changed`
    refused) keeps the person's consent, the last summary the server took with its digest and a refusal still waited
    out, so a renewal changes nothing the person sees; a rotation does not write the state afresh and keeps them too.
    A new token's enrollment never carries the consent (OBS-221): the token may enroll another person, and a machine
    removed or an organization closed cleared it already. It sends the summary only after consent is given again.
    `previous` is always this organization's: the state lives in the organization's own folder."""
    if not isinstance(previous, dict):
        return {}
    out: Dict[str, Any] = {}
    sequence = previous.get('summarySequence')
    if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > 0:
        out['summarySequence'] = sequence
    if renewal:
        for key in ('summaryConsent', 'nodeSummary', 'summaryRefused', 'summaryBackoffUntil'):
            if key in previous:
                out[key] = previous[key]
    return out


def summary_notice(state: dict) -> dict:
    """The node summary's state for a person (OBS-192), from the saved state alone: whether the server reads
    `node-summary/1` (`listed`), the last one it took (`sentAt`, `sequence`, how much it listed, and its drift when the
    organization's baseline applied: `drift` is null without one), and why the latest was not built (`note`)."""
    last = state.get('nodeSummary') if isinstance(state.get('nodeSummary'), dict) else None
    out: Dict[str, Any] = {'listed': SUMMARY_PROTOCOL in (state.get('protocols') or []),
                           'sentAt': last.get('sentAt') if last else None,
                           'sequence': last.get('sequence') if last else None,
                           'counts': last.get('counts') if last else None,
                           'drift': last.get('drift') if last else None}
    consent = state.get('summaryConsent') if isinstance(state.get('summaryConsent'), dict) else {}
    out['consent'] = {'on': consent.get('on') is True, 'at': consent.get('at')}
    note = state.get('nodeSummaryNote')
    if isinstance(note, dict):
        out['note'] = {k: note[k] for k in ('at', 'reason', 'code', 'detail') if k in note}
    refused = state.get('summaryRefused')
    if isinstance(refused, dict):
        out['refused'] = {k: refused[k] for k in ('at', 'status', 'code', 'retryAt') if k in refused}
    if not out['listed']:
        out['state'] = 'not-listed'
    elif not out['consent']['on']:
        out.update(state='consent-off', said='harness summary: not shared (consent off)')
    elif str((out.get('note') or {}).get('reason') or '').startswith('node-summary-'):
        out['state'] = 'unavailable'
    elif 'refused' in out:
        out['state'] = 'refused'
    else:
        out['state'] = 'sent' if out['sentAt'] else 'pending'
    return out


def node_folder(home: Path) -> Path:
    """The node folder `full org` and the maintenance job use: `OBSERVATORY_NODE`, else `<home>/node`."""
    return Path(os.environ.get('OBSERVATORY_NODE') or Path(home) / 'node').expanduser()


def notices(node: Path, now: Optional[float] = None) -> List[dict]:
    """Every enrollment's `notice`, read-only: nothing is created, locked or sent. An unreadable one is named, never
    skipped."""
    now = time.time() if now is None else now
    out = []
    try:
        orgs = enrolled_orgs(node)
    except OSError as error:                               # an unreadable node folder is a state, not a crash
        return [{'org': None, 'state': 'unreadable', 'reason': type(error).__name__}]
    for org_id in orgs:
        try:
            state = Enrollment(Path(node).resolve(), org_id).state()
        except (OSError, ValueError, RuntimeError) as error:
            out.append({'org': org_id, 'state': 'unreadable', 'reason': type(error).__name__})
            continue
        if state is not None:
            out.append(notice(org_id, state, now))
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):           # a signed body is never re-sent to another target
        return None


def default_transport(url: str, headers: Dict[str, str], body: bytes, timeout: float = 30) -> Tuple[int, bytes]:
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, data=body, method='POST', headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, error.read(64 * 1024)


def ulid(ms: Optional[int] = None) -> str:
    """A ULID (Crockford base32, 26 characters): 48 bits of milliseconds, 80 random bits."""
    t = int(time.time() * 1000) if ms is None else ms
    head = ''.join(_CROCKFORD[(t >> (5 * i)) & 31] for i in reversed(range(10)))
    rand = int.from_bytes(secrets.token_bytes(10), 'big')
    return head + ''.join(_CROCKFORD[(rand >> (5 * i)) & 31] for i in reversed(range(16)))


def iso(seconds: float) -> str:
    return _dt.datetime.fromtimestamp(int(seconds), _dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def platform_name() -> str:
    return {'darwin': 'macos', 'win32': 'windows'}.get(sys.platform, 'linux' if sys.platform.startswith('linux')
                                                        else 'other')


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None


def _protocols(doc: Any) -> Optional[List[str]]:
    """The protocol list an answer carries (DEC-0067), or None when it carries none: the held list is replaced only by
    a list that arrives (DEC-0094)."""
    value = doc.get('protocols') if isinstance(doc, dict) else None
    return [p for p in value if isinstance(p, str)] if isinstance(value, list) else None


def _key_schema_refusal(status: int, code: str, answer: Any) -> bool:
    """A 422 schema refusal whose detail points at `/key/storage` or `/key/exportable`: what a server on a contract
    before CT-65 (DEC-0094, which admits `storage: file`) answers a file-held key. Named `key-storage-refused`, not a generic
    enrollment refusal, because a new token would not help: the server or its contract has to change."""
    if status != 422 or ('schema' not in code and code not in ('invalid-request', 'request-invalid', 'validation')):
        return False
    error = answer.get('error') if isinstance(answer, dict) else None
    detail = json.dumps({k: v for k, v in error.items() if k != 'code'}) if isinstance(error, dict) else ''
    return bool(re.search(r'/key/(storage|exportable)\b', detail))


def _summary_suspect(status: int, answer: Any) -> bool:
    """Whether a refused check-in that carried a summary may have been refused for the summary: a malformed or too
    large request (400, 413, 422) whatever it says, or another client error whose code, pointer or message names the
    summary or the records. A signature, certificate or rate refusal is the check-in's own."""
    if status in SUMMARY_SUSPECT_STATUSES:
        return True
    if not 400 <= status < 500 or status in (401, 408, 425, 429):
        return False
    error = answer.get('error') if isinstance(answer, dict) and isinstance(answer.get('error'), dict) else {}
    said = ' '.join(str(error.get(k) or '') for k in ('code', 'pointer', 'message')).lower()
    return 'summary' in said or 'records' in said


def organization_closed_at(status: int, body: Any) -> Optional[str]:
    """`closedAt` when an answer is DEC-0103's `organization-closed`: HTTP 410 and exactly the body
    `device-organization-closed.schema.json` admits (`{error: {code: organization-closed, message}, closedAt}`, the
    message one path-free line, `closedAt` an RFC 3339 date-time); None for anything else."""
    if status != 410 or not isinstance(body, dict) or set(body) != {'error', 'closedAt'}:
        return None
    error, closed_at = body['error'], body['closedAt']
    if not isinstance(error, dict) or set(error) != {'code', 'message'} or error['code'] != ORGANIZATION_CLOSED:
        return None
    if not isinstance(error['message'], str) or not DETAIL.match(error['message']):
        return None
    return closed_at if isinstance(closed_at, str) and strict_time(closed_at) is not None else None


def _error_code(doc: Any) -> Optional[str]:
    """The receiver's refusal code, bounded: `{error: {code}}` or `{error: code}` or `{code}`."""
    if isinstance(doc, dict):
        err = doc.get('error')
        code = err.get('code') if isinstance(err, dict) else err if isinstance(err, str) else doc.get('code')
        if isinstance(code, str) and re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', code):
            return code
    return None


def read_token(slot: str) -> str:
    """The enrollment token from the vault slot `PROJECT/ENV/NAME`, vault only (never an env file)."""
    parts = slot.split('/')
    if len(parts) != 3 or not all(parts):
        raise OrgError('token-slot', 'the token slot is PROJECT/ENV/NAME')
    tools = Path(__file__).resolve().parents[1] / 'tools'
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    try:
        import use_secret
        value, _ = use_secret.resolve(parts[0], parts[2], parts[1], vault_only=True)
    except LookupError:
        raise OrgError('token-missing', 'the vault holds no value in that slot') from None
    value = (value or '').strip()
    if not TOKEN.fullmatch(value):
        raise OrgError('token-malformed', 'the slot does not hold an enrollment token of the form <tokenId>.<secret>')
    return value


# ---- What this machine applied of its estate record -----------------------------------------------------------

def records_folder(node: Path) -> Path:
    return Path(node) / 'records'


def applied_records(node: Path) -> Tuple[Optional[dict], List[dict]]:
    """The estate revision this machine verified and applied, and the revocation-delta entries it applied since
    (`records/estate-record.json`, `records/delta-state.json`), or `(None, [])`."""
    folder = records_folder(node)
    try:
        record = json.loads(private_io.read(folder / 'estate-record.json'))
    except (FileNotFoundError, ValueError, RuntimeError, OSError):
        return None, []
    try:
        state = json.loads(private_io.read(folder / 'delta-state.json'))
        entries = [e for e in state.get('entries', []) if isinstance(e, dict)] if isinstance(state, dict) else []
    except (FileNotFoundError, ValueError, RuntimeError, OSError):
        entries = []
    return (record if isinstance(record, dict) else None), entries


def apply_record(node: Path, record: Any, at_ms: int, authorities: Optional[list] = None) -> dict:
    """Verifies an estate revision (`verify_estate_record`) against the one applied before it, or, for the first,
    under the passkeys the operator pinned (`authorities`), and stores it as applied. A draft or a revision that does
    not verify is refused and nothing changes."""
    from . import estate
    folder = records_folder(node)
    current, _ = applied_records(node)
    if current is not None:
        if isinstance(record, dict) and record.get('revision') == current.get('revision') \
                and record.get('digest') == current.get('digest'):
            return {'ok': True, 'revision': current.get('revision'), 'unchanged': True}
        verdict = estate.verify_estate_record(record, at_ms=at_ms, parent=current)
    else:
        if not authorities:
            raise OrgError('no-authorities', 'the first revision this machine applies needs the passkeys it pins')
        verdict = estate.verify_estate_record(record, at_ms=at_ms, authorities=authorities)
    if not verdict.get('ok'):
        raise OrgError('record-refused', 'the estate revision does not verify: %s' % verdict.get('problem'))
    with private_io.lock(folder / '.records.lock'):
        private_io.write(folder / 'estate-record.json', json.dumps(record, sort_keys=True))
        state_path = folder / 'delta-state.json'
        if current is None or current.get('estate') != record.get('estate'):
            from .revocation import empty_delta_state
            private_io.write(state_path, json.dumps(empty_delta_state(record.get('estate'))))
    return {'ok': True, 'revision': verdict.get('revision'), 'digest': verdict.get('digest')}


def apply_delta_page(node: Path, page: Any, at_ms: int) -> dict:
    """Applies a page of revocation-delta entries (`revocation.apply_delta`) under the applied revision."""
    from . import revocation
    record, _ = applied_records(node)
    if record is None:
        raise OrgError('no-record', 'apply an estate revision before its delta')
    folder = records_folder(node)
    with private_io.lock(folder / '.records.lock'):
        try:
            state = json.loads(private_io.read(folder / 'delta-state.json'))
        except (FileNotFoundError, ValueError, RuntimeError, OSError):
            state = revocation.empty_delta_state(record.get('estate'))
        result = revocation.apply_delta(state, page, {'record': record, 'at': at_ms})
        private_io.write(folder / 'delta-state.json', json.dumps(result.get('state', state)))
    if not result.get('ok'):
        raise OrgError('delta-refused', 'the delta page was refused: %s' % result.get('problem'))
    return {'ok': True, 'applied': result.get('applied', 0)}


# ---- One enrollment ---------------------------------------------------------------------------------------------

def _upgraded(doc: dict) -> Tuple[dict, Optional[dict]]:
    """A state from 0.29.0's retired provisional rule, upgraded (OBS-221): an enrollment that rule ended
    (`removed.code` `unknown-org`) is active again and checks in at once, and the rule's `tickSeq` and `unknownOrg`
    records go. Answers the same object when there is nothing to upgrade, else a copy and the removal it undid. The
    events that rule dropped stay dropped: their keys are gone and the server never took them."""
    removed = doc.get('removed')
    undone = removed if isinstance(removed, dict) and removed.get('code') == UNKNOWN_ORG else None
    if undone is None and 'tickSeq' not in doc and 'unknownOrg' not in doc:
        return doc, None
    out = {k: v for k, v in doc.items() if k not in ('tickSeq', 'unknownOrg')}
    if undone is not None:
        out.pop('removed', None)
        # The removal set `reEnroll` without a reason. A reason recorded before it still stands, with `reEnroll`:
        # only `epoch-superseded` or `certificate-expired` (`_ends`, from a rotation or flush refusal) can be one, since
        # a check-in's `certificate.action: re-enroll` sets `reEnroll` and drops any reason.
        out.update(reEnroll=isinstance(out.get('reEnrollReason'), str), nextCheckInAt=0)
    return out, undone


def _same_binding(held: dict, cert: dict) -> bool:
    """Whether a renewed certificate binds what the one on record binds (DEC-0100, FAC-SEM-044, the contract's
    `device-rules.ts` renewal check): a person or none, the same person by the binding's user id, and the same role.
    A record with a user id needs the same id in the renewal; none, or another, is a changed binding. Only a record
    saved by 0.29.0, which kept whether a person was bound but not who, is compared by that alone, and the renewal
    records the id (OBS-221). `_certificate` refuses a person-bound certificate without an id."""
    if bool(cert.get('user')) != bool(held.get('user')) or ('role' in held and cert.get('role') != held.get('role')):
        return False
    if 'userId' in held:
        return cert.get('userId') == held['userId']
    return True


#: The layers a check-in reports (`policy.layers`, at most two, the server's always: devices.md).
POLICY_SOURCES = ('server', 'mdm')
#: The largest revision a `device-policy` may carry (its schema's maximum, JSON's safe integer).
POLICY_REVISION_MAX = 9007199254740991


def _policy_arrived(state: dict, policy: Any, clock: float, org_id: str) -> Optional[dict]:
    """Records a signed policy a check-in response carried (`serverPolicy`, per source) when its `kind`, `source`,
    `revision` and `org` read as a `device-policy` of this organization newer than any that arrived before, and returns
    what was recorded, or None. Its signature, `issuedAt` and keys are not read here. Nothing is verified or
    applied here: verifying a layer's signature against the organization's key set and applying it is OE-06
    (`policy_layers`). The record exists so the next check-in says a policy arrived and is not in force (OBS-231),
    instead of the revision 0 `applied` that means no policy ever arrived."""
    if not isinstance(policy, dict) or policy.get('kind') != 'policy' or policy.get('source') not in POLICY_SOURCES:
        return None
    if not isinstance(policy.get('org'), dict) or policy['org'].get('id') != org_id:
        return None
    revision = policy.get('revision')
    if not isinstance(revision, int) or isinstance(revision, bool) or not 1 <= revision <= POLICY_REVISION_MAX:
        return None
    held = state.get('serverPolicy') if isinstance(state.get('serverPolicy'), dict) else {}
    before = held.get(policy['source']) if isinstance(held.get(policy['source']), dict) else {}
    if isinstance(before.get('revision'), int) and revision <= before['revision']:
        return None
    held[policy['source']] = {'revision': revision, 'at': iso(clock)}
    state['serverPolicy'] = held
    return {'source': policy['source'], 'revision': revision}


def policy_report(state: dict) -> List[dict]:
    """`policy.layers` of a check-in (contract devices.md): for each delivered layer the revision this machine holds
    and its status. This node verifies and applies no signed policy yet (OE-06), so it holds revision 0 of every layer.
    The server layer is always reported: `applied` at revision 0 is the contract's word for "none arrived". A layer a
    response carried is `failed`, its error naming the revision that arrived, so no server reads a policy as in force
    on this machine (OBS-231, fabric-workspace X-7). A held revision out of the schema's range (a damaged enrollment
    file) is read as none, so the check-in stays valid."""
    held = state.get('serverPolicy') if isinstance(state.get('serverPolicy'), dict) else {}
    layers = []
    for source in POLICY_SOURCES:
        arrived = held.get(source) if isinstance(held.get(source), dict) else None
        revision = arrived.get('revision') if arrived is not None else None
        if not isinstance(revision, int) or isinstance(revision, bool) or not 1 <= revision <= POLICY_REVISION_MAX:
            if source == 'server':
                layers.append({'source': source, 'revision': 0, 'status': 'applied'})
            continue
        layers.append({'source': source, 'revision': 0, 'status': 'failed',
                       'error': 'signed policy revision %d arrived; this node version does not verify or apply '
                                'policy yet, so none is in force' % revision})
    return layers


def _policy_carried(previous: Optional[dict], renewal: bool) -> dict:
    """A renewal is the same enrollment: the policy that arrived for it stays reported. A new enrollment starts with
    none (a removal or a closed organization discards it, DEC-0103)."""
    if renewal and isinstance(previous, dict) and isinstance(previous.get('serverPolicy'), dict):
        return {'serverPolicy': previous['serverPolicy']}
    return {}


class Enrollment:
    """One organization enrollment of this machine: its state, outbox, requests and schedule."""

    def __init__(self, node: Path, org_id: str, *, keys=None, transport: Transport = default_transport,
                 clock: Callable[[], float] = time.time, meter_db: Optional[Path] = None,
                 paths: Optional[Dict[str, str]] = None,
                 harness: Optional[Callable[[], Tuple[Any, Any, Optional[str]]]] = None,
                 exclusions: Optional[Callable[[], Tuple[List[str], Optional[str]]]] = None):
        if not isinstance(org_id, str) or not ORG_ID.fullmatch(org_id):
            raise OrgError('org-id', 'an organization id is [A-Za-z0-9][A-Za-z0-9_-]{0,127}')
        self.node, self.org_id = Path(node), org_id
        self.folder = self.node / 'orgs' / org_id
        self._keys, self.transport, self.clock, self.meter_db = keys, transport, clock, meter_db
        self.paths = dict(PATHS, **(paths or {}))
        self.harness = harness or (lambda: read_harness_scans(self.clock()))
        self.exclusions = exclusions or read_exclusions
        self._held = threading.local()                     # re-entry is per thread: another thread waits

    # -- the lock ----------------------------------------------------------------------------------------------
    @contextlib.contextmanager
    def _locked(self):
        """One writer per enrollment at a time, across processes (the CLI and the maintenance job): every command that
        changes the state or the outbox runs under it and reads the state again inside it, so a check-in can never save
        a copy older than a rotation's certificate, and a tick never recreates what `leave` removed. The lock file is
        outside the enrollment's folder, so removing the folder never frees a lock another process waits on."""
        depth = getattr(self._held, 'depth', 0)
        if depth:
            self._held.depth = depth + 1
            try:
                yield
            finally:
                self._held.depth = depth
            return
        with private_io.lock(self.node / 'orgs' / '.locks' / (self.org_id + '.lock')):
            self._held.depth = 1
            try:
                yield
            finally:
                self._held.depth = 0

    # -- state -------------------------------------------------------------------------------------------------
    @property
    def keys(self):
        """This enrollment's own device keys (devices.md: each organization enrollment of a machine gets keys of its
        own), in `<node>/orgs/<org>/keys/`: a device id and `#sig`/`#kem` pair no other organization sees."""
        if self._keys is None:
            from .device_keys import DeviceKeys
            self._keys = DeviceKeys.load_or_create(self.node.resolve() / 'orgs' / self.org_id / 'keys')
        return self._keys

    def state(self) -> Optional[dict]:
        """The saved state. One written by 0.29.0 under the retired provisional `unknown-org` rule (an enrollment it
        ended with `removed: {code: "unknown-org"}`, and its `tickSeq` counter) reads as DEC-0103 has it: active,
        because `unknown-org` on a device request is any failure (`_upgraded`). The read itself changes nothing on
        disk; under the enrollment's lock (every writer reads there) the upgrade is saved once, with a journal line."""
        doc = self._stored()
        if doc is None:
            return None
        upgraded, undone = _upgraded(doc)
        if upgraded is not doc and getattr(self._held, 'depth', 0):
            self._save(upgraded)
            if undone is not None:
                self.journal('removal-undone', code=UNKNOWN_ORG, removedAt=undone.get('at'),
                             droppedEvents=undone.get('droppedEvents'))
        return upgraded

    def _stored(self) -> Optional[dict]:
        """The state file as written, without `state()`'s upgrade."""
        try:
            doc = json.loads(private_io.read(self.folder / 'enrollment.json'))
        except FileNotFoundError:
            return None
        return doc if isinstance(doc, dict) and doc.get('schema') == 'org-enrollment/1' else None

    def _save(self, doc: dict) -> None:
        private_io.write(self.folder / 'enrollment.json', json.dumps(doc, indent=1, sort_keys=True) + '\n')

    def journal(self, event: str, **fields: Any) -> None:
        """One structured line: what happened, codes and counts, never a token, key or body."""
        line = json.dumps({'at': iso(self.clock()), 'event': event, 'org': self.org_id, **fields}, sort_keys=True)
        LOG.info(line)
        path = self.folder / 'journal.jsonl'
        try:
            private_io.parent(self.folder)
            if path.exists() and path.stat().st_size > JOURNAL_BYTES:
                kept = path.read_text(encoding='utf-8').splitlines()[-200:]
                private_io.write(path, '\n'.join(kept) + '\n')
            fd = osprivacy.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | osprivacy.NOFOLLOW, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as stream:
                stream.write(line + '\n')
        except OSError:
            pass

    @contextlib.contextmanager
    def _db(self):
        private_io.parent(self.folder)
        path = self.folder / 'outbox.sqlite'
        fresh = not path.exists()
        conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            if fresh:
                os.chmod(path, 0o600)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (epoch INTEGER NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL
                  UNIQUE, kind TEXT NOT NULL, key TEXT UNIQUE, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                  reason TEXT, PRIMARY KEY (epoch, seq));
                CREATE TABLE IF NOT EXISTS registrations (grant_id TEXT PRIMARY KEY, doc TEXT NOT NULL, digest TEXT
                  NOT NULL, answer TEXT, gate TEXT NOT NULL DEFAULT 'hold', record_digest TEXT, sent_at INTEGER,
                  attempts INTEGER NOT NULL DEFAULT 0, next_at INTEGER NOT NULL DEFAULT 0,
                  active_registered INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);""")
            with contextlib.suppress(sqlite3.OperationalError):    # an outbox from before the column
                conn.execute('ALTER TABLE registrations ADD COLUMN active_registered INTEGER NOT NULL DEFAULT 0')
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _meta(conn, name: str, default: Any = None) -> Any:
        row = conn.execute('SELECT value FROM meta WHERE name = ?', (name,)).fetchone()
        return json.loads(row['value']) if row else default

    @staticmethod
    def _set_meta(conn, name: str, value: Any) -> None:
        conn.execute('INSERT INTO meta(name, value) VALUES(?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value',
                     (name, json.dumps(value, sort_keys=True)))

    def _backoff(self, attempts: int) -> int:
        return int(self.clock()) + min(BACKOFF_CEILING, BACKOFF_FLOOR * 2 ** min(attempts, 10)) + random.randint(0, 15)

    # -- requests ----------------------------------------------------------------------------------------------
    def _post(self, origin: str, path: str, doc: dict, headers: Dict[str, str]) -> Tuple[int, Any]:
        body = json.dumps(doc, separators=(',', ':')).encode('utf-8')
        try:
            status, raw = self.transport(origin + path, {'Content-Type': 'application/json', **headers}, body, 30)
        except (OSError, ValueError) as error:
            self.journal('transport-failed', path=path, error=type(error).__name__)
            return 0, None
        return status, _json(raw)

    def _signed(self, state: dict, path: str, doc: dict,
                rebuild: Optional[Callable[[dict], dict]] = None) -> Tuple[int, Any]:
        """A device request signed afresh (DEC-0056): a new nonce and `created` on every attempt, `created` on the
        server's clock as the last check-in measured it (`skewSeconds`). A `signature-stale` refusal forgets that
        measure and asks for a check-in now, which measures it again. The first answer past the signature check under
        a rotated certificate lets the previous certificate go (CT-65); a `certificate-unknown` before that goes back
        to the previous one, which the server keeps valid until the new serial is first presented. A refusal that
        says the machine is no longer in the organization (`REMOVED_CODES`, OBS-204) or that the organization ended
        (`ORGANIZATION_CLOSED`, DEC-0103) ends the enrollment here, for every caller; a removed enrollment sends
        nothing."""
        if isinstance(state.get('removed'), dict):
            raise self._removed_error(state)
        body = json.dumps(doc, separators=(',', ':')).encode('utf-8')
        created = int(self.clock() + (state.get('skewSeconds') or 0))
        headers = http_signature.sign_request(self.keys.sign, method='POST', origin=state['server'], target=path,
                                              body=body, serial=state['certificate']['serial'], created=created)
        try:
            status, raw = self.transport(state['server'] + path, {'Content-Type': 'application/json', **headers},
                                         body, 30)
        except (OSError, ValueError) as error:
            self.journal('transport-failed', path=path, error=type(error).__name__)
            return 0, None
        answer = _json(raw)
        if status == 401:
            code = _error_code(answer)
            self.journal('request-refused', path=path, status=401, code=code)
            previous = state.get('previousCertificate')
            if code == 'signature-stale':
                state['skewSeconds'] = 0
                state['nextCheckInAt'] = 0
                self._save(state)
            elif code == 'certificate-unknown' and isinstance(previous, dict) \
                    and self.clock() + (state.get('skewSeconds') or 0) < strict_time(previous['notAfter']) / 1000:
                # The server does not know the rotated certificate; the previous one is still valid (CT-65). Back to
                # it, and the same request once more under it: only a refusal of that one can end the enrollment.
                state['certificate'] = state.pop('previousCertificate')
                self._save(state)
                self.journal('certificate-fallback', serial=state['certificate']['serial'])
                return self._signed(state, path, rebuild(state) if rebuild else doc)
        elif 200 <= status < 300 and state.get('previousCertificate'):
            state.pop('previousCertificate', None)         # a 2xx proves the server took the new serial
            self._save(state)
        code = _error_code(answer)
        closed_at = organization_closed_at(status, answer)
        if closed_at is not None:                          # DEC-0103: final on one answer
            self._remove(state, ORGANIZATION_CLOSED, status, closed_at=closed_at)
        elif status == 401 and code in REMOVED_CODES:      # only with 401, as the contract sends them
            self._remove(state, code, status)
        return status, answer

    def _remove(self, state: dict, code: str, status: int, closed_at: Optional[str] = None) -> None:
        """The organization no longer has this machine (OBS-204): its pending events can be accepted nowhere, so they
        are dropped with one count recorded, the retry and in-flight batch are forgotten, and the state says when and
        why. `state` is the caller's own copy, changed in place, so what it saves next keeps the removal.

        With `closed_at` the organization ended (DEC-0103, `organization-closed`): what was buffered for it goes
        whole, the stream's rows, the share registrations and their answers (the sums they gated stay in the meter,
        which belongs to no organization), and so do the organization's server policy layer, key set and collection
        start. An `mdm` layer is the operating system's and stays; the machine's other enrollments are untouched. A
        removal on `certificate-revoked` or `certificate-unknown` keeps what OBS-204 keeps (the contract does not
        decide that end)."""
        if isinstance(state.get('removed'), dict):
            return
        with self._db() as conn:
            dropped = conn.execute("UPDATE events SET state = 'dropped', key = NULL, reason = ? WHERE state = 'pending'",
                                   (('removed:' + code)[:40],)).rowcount
            conn.execute("DELETE FROM meta WHERE name IN ('retry', 'inflight')")
            registrations = 0
            if closed_at is not None:
                registrations = conn.execute('DELETE FROM registrations').rowcount
                conn.execute('DELETE FROM events')
                conn.execute('DELETE FROM meta')
        state['removed'] = {'at': iso(self.clock()), 'code': code, 'status': status, 'droppedEvents': dropped}
        if closed_at is not None:
            state['removed'].update(closedAt=closed_at, droppedRegistrations=registrations)
            for held in ('collectionStart', 'policyRootKey', 'keySet', 'serverPolicy', 'purposeHolds',
                         'purposeRefused', 'acknowledgedNoticeVersion', 'noticeVersion'):
                state.pop(held, None)
        state.pop('unknownOrg', None)                      # the provisional rule's record, before DEC-0103
        state.pop('tickSeq', None)
        # The person's consent to the harness summary ends with the enrollment (OBS-221): a machine removed or an
        # organization closed sends nothing, and an enrollment that follows asks the person again.
        state.pop('summaryConsent', None)
        state['reEnroll'] = True
        state.pop('pendingRotation', None)
        self._save(state)
        self.journal('removed-from-organization', code=code, status=status, droppedEvents=dropped,
                     **({'closedAt': closed_at, 'droppedRegistrations': registrations} if closed_at else {}))

    def _removed_error(self, state: dict) -> 'OrgError':
        said = notice(self.org_id, state, self.clock())
        return OrgError('removed-from-organization', 'this machine was removed from the organization on %s (%s)'
                        % (state['removed'].get('at'), state['removed'].get('code')),
                        next_step=said.get('nextStep'), command=said.get('command'))
    def _certificate(self, response: Any, epoch_expected: Optional[int] = None) -> dict:
        """The certificate an enrollment or rotation answered, checked against what was asked: this organization and
        device, a serial, a window, and the machine's own `#sig` key in the DER."""
        cert = response.get('certificate') if isinstance(response, dict) else None
        if not isinstance(cert, dict):
            raise OrgError('response-malformed', 'the answer carries no certificate')
        binding = cert.get('binding') if isinstance(cert.get('binding'), dict) else {}
        if (binding.get('org') or {}).get('id') != self.org_id or (binding.get('device') or {}).get('id') \
                != self.keys.device_id:
            raise OrgError('binding-mismatch', 'the certificate binds another organization or device')
        serial, start, end = cert.get('serial'), strict_time(cert.get('notBefore')), strict_time(cert.get('notAfter'))
        if not isinstance(serial, str) or not http_signature.SERIAL.fullmatch(serial) or start is None \
                or end is None or end <= start:
            raise OrgError('response-malformed', 'the certificate has no usable serial or window')
        if end - start > 30 * 86400 * 1000:
            raise OrgError('certificate-too-long', 'a device certificate lives at most 30 days (FAC-SEM-044)')
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import serialization
            der = base64.b64decode(cert.get('der', ''), validate=True)
            spki = x509.load_der_x509_certificate(der).public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        except (ValueError, TypeError) as error:
            raise OrgError('response-malformed', 'the certificate is not DER') from error
        if spki != self.keys.sig_spki():
            raise OrgError('key-mismatch', "the certificate does not certify this machine's #sig key")
        # The person the certificate binds, as the contract's binding carries it (`activity-common` `user`: an
        # object whose `id` is an opaque membership id, required): a renewal compares it, so a certificate re-issued
        # to another person is `binding-changed` (OBS-221). A `user` without such an id is refused whole: kept, it
        # would drop the id from the record and let the next renewal bind anyone.
        user = binding.get('user')
        if user is not None and not (isinstance(user, dict) and isinstance(user.get('id'), str)
                                     and OPAQUE_ID.fullmatch(user['id'])):
            raise OrgError('response-malformed', 'the certificate binds a user without an opaque id')
        out = {'serial': serial, 'notBefore': cert['notBefore'], 'notAfter': cert['notAfter'],
               'issuer': cert.get('issuer'), 'user': user is not None}
        if user is not None:
            out['userId'] = user['id']
        if isinstance(binding.get('role'), str):           # `org-cloud` (DEC-0099): a renewal keeps it (DEC-0100)
            out['role'] = binding['role']
        return out

    # -- enrollment --------------------------------------------------------------------------------------------
    def enrollment_request(self, token_id: Optional[str], at: Optional[float] = None) -> dict:
        """An enrollment request with an enrollment token's id, or, with none, a renewal by the key on record
        (DEC-0100): `auth` is then `{method: key-on-record}` alone and the CSR's signature is the proof."""
        spki = self.keys.sig_spki()
        auth = {'method': 'enrollment-token', 'tokenId': token_id} if token_id is not None else \
            {'method': 'key-on-record'}
        return {'protocol': DEVICE_PROTOCOL, 'kind': 'enrollment-request', 'org': {'id': self.org_id},
                'device': {'id': self.keys.device_id, 'platform': platform_name()},
                'key': {'algorithm': 'ecdsa-p256', 'storage': getattr(self.keys, 'storage', 'file'),
                        'exportable': bool(getattr(self.keys, 'exportable', True)),
                        'publicKeySha256': 'sha256:' + hashlib.sha256(spki).hexdigest()},
                'deviceKeys': self.keys.enrollment_keys(),
                'csr': base64.b64encode(self.keys.csr()).decode('ascii'),
                'auth': auth, 'requestedAt': iso(self.clock() if at is None else at)}
    def enroll(self, server: str, token: str) -> dict:
        origin = http_signature.public_origin(server)
        if origin is None:
            raise OrgError('server-invalid', 'the server is https:// and an authority alone')
        match = TOKEN.fullmatch(token or '')
        if not match:
            raise OrgError('token-malformed', 'an enrollment token is <tokenId>.<secret>')
        with self._locked():
            # The stored state as written, not upgraded: an enrollment 0.29.0 ended on `unknown-org` is still a
            # removal for a new token (OBS-221), as 0.29.0 treated it, so an organization recreated under the same
            # id, whose epochs start again, is accepted.
            previous = self._stored()
            # A machine removed from its organization starts over with a new token (OBS-204): the organization it
            # joins may be a new one whose epochs start again, and nothing of the old stream is carried.
            over = previous is not None and isinstance(previous.get('removed'), dict)
            status, answer = self._post(origin, self.paths['enroll'], self.enrollment_request(match.group(1)),
                                        {'Authorization': 'EnrollmentToken ' + token})
            if status != 200 or not isinstance(answer, dict) or answer.get('kind') != 'enrollment-response':
                code = _error_code(answer) or ('unreachable' if status == 0 else 'http-%d' % status)
                self.journal('enroll-refused', status=status, code=code)
                if code in STORAGE_REFUSALS or 'storage' in code or _key_schema_refusal(status, code, answer):
                    raise OrgError('key-storage-refused', "the server's policy refuses this machine's %s-held device "
                                   'key: %s (HTTP %s)' % (getattr(self.keys, 'storage', 'file'), code, status))
                if code == UNKNOWN_ORG:                  # the server's own enrollment code, not the contract's
                    raise OrgError('enroll-refused', 'the server does not know organization %s (%s, HTTP %s): check '
                                   'the organization id and the server with its administrator'
                                   % (self.org_id, code, status))
                raise OrgError('enroll-refused', 'the server refused the enrollment: %s (HTTP %s)' % (code, status))
            self._accept(origin, answer, previous, over)
            self._register_quietly()
            return self.describe()

    def _accept(self, origin: str, answer: dict, previous: Optional[dict], over: bool,
                renewed: Optional[dict] = None) -> dict:
        """Takes an enrollment's answer (a token's, or a renewal's by the key on record, `renewed`): the certificate
        is checked, the epoch is above every one issued before, and the state is written afresh. Under the lock."""
        cert = self._certificate(answer)
        if renewed is not None and previous is not None:
            # DEC-0100 (FAC-SEM-044): a renewal keeps the binding on record, the same user (or none) and role.
            held = previous['certificate']
            if not _same_binding(held, cert):
                raise OrgError('binding-changed', 'the renewed certificate binds another user or role than the one '
                               'on record')
        epoch = answer.get('streamEpoch')
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1 \
                or (previous and not over and epoch <= previous.get('epoch', 0)):
            raise OrgError('epoch-invalid', 'the stream epoch is not above every epoch issued to this device')
        interval = (answer.get('checkIn') or {}).get('intervalSeconds')
        doc = {'schema': 'org-enrollment/1', 'org': self.org_id, 'device': self.keys.device_id, 'server': origin,
               'certificate': cert, 'epoch': epoch, 'protocols': _protocols(answer) or [],
               'checkInSeconds': interval if isinstance(interval, int) and 30 <= interval <= 86400 else 300,
               'collectionStart': answer.get('collectionStart') if strict_time(answer.get('collectionStart'))
               is not None else None,
               'policyRootKey': answer.get('policyRootKey'), 'enrolledAt': iso(self.clock()),
               'sequenceNum': 0, 'fullState': False, 'lastCheckIn': None, 'rotate': False,
               'reEnroll': False, 'nextCheckInAt': 0, 'skewSeconds': 0}
        if over:
            doc['previousRemoval'] = previous['removed']
        if renewed is not None and previous is not None:
            # The same enrollment, renewed: when it began stays. DEC-0102 item 6: a renewal is judged on the server's
            # clock, which the new certificate's `notBefore` reads, never this machine's: it is when the renewal took
            # effect, and the server's clock is measured again from it (kept as last measured when it is a day off).
            issued = strict_time(cert['notBefore']) / 1000
            skew = round(issued - self.clock())
            doc.update(enrolledAt=previous.get('enrolledAt') or doc['enrolledAt'], renewedAt=cert['notBefore'],
                       skewSeconds=skew if abs(skew) <= 86400 else previous.get('skewSeconds') or 0)
            # The same person under the same notice: a purpose hold and the notice versions it waits on stay (DEC-0100).
            doc.update({k: previous[k] for k in ('purposeHolds', 'acknowledgedNoticeVersion', 'noticeVersion')
                        if k in previous})
        doc.update(_summary_carried(previous, renewal=renewed is not None))
        doc.update(_policy_carried(previous, renewal=renewed is not None))
        private_io.parent(self.folder)
        self._save(doc)
        with self._db() as conn:                       # a new enrollment: registrations are sent again
            if over:                                   # nothing pending survived the removal; start a new stream
                conn.execute('DELETE FROM events')
                conn.execute('DELETE FROM registrations')
                conn.execute('DELETE FROM meta')
            conn.execute("UPDATE registrations SET next_at = 0, sent_at = NULL WHERE gate != 'hold-degraded'")
        if renewed is not None:
            self.journal('renewed', by='key-on-record', epoch=epoch, serial=cert['serial'], notAfter=cert['notAfter'],
                         expired=renewed.get('expired'), protocols=doc['protocols'])
        else:
            self.journal('enrolled', epoch=epoch, serial=cert['serial'], notAfter=cert['notAfter'],
                         protocols=doc['protocols'], reEnrollment=previous is not None, afterRemoval=over)
        return doc

    def rotate(self) -> dict:
        """A new certificate for the same key. The request is persisted before it is sent and sent again, unchanged,
        after a lost answer: the server answers the same request with the same certificate (CT-65)."""
        with self._locked():
            state = self._require()
            pending = state.get('pendingRotation')
            if not isinstance(pending, dict) or pending.get('serial') != state['certificate']['serial']:
                pending = {'serial': state['certificate']['serial'], 'at': iso(self.clock()),
                           'request': {'protocol': DEVICE_PROTOCOL, 'kind': 'rotation-request',
                                       'device': {'id': self.keys.device_id},
                                       'csr': base64.b64encode(self.keys.csr()).decode('ascii')}}
                state['pendingRotation'] = pending
                self._save(state)
            status, answer = self._signed(state, self.paths['rotate'], pending['request'])
            if status != 200 or not isinstance(answer, dict):
                code = _error_code(answer) or ('unreachable' if status == 0 else 'http-%d' % status)
                self.journal('rotate-refused', status=status, code=code)
                if _ends(status, code):
                    state['reEnroll'], state['reEnrollReason'] = True, code
                    state.pop('pendingRotation', None)
                elif status in (400, 422):                 # refused for what it is: the next rotation asks anew
                    state.pop('pendingRotation', None)
                self._save(state)
                raise OrgError('rotate-refused', 'the server refused the rotation: %s (HTTP %s)' % (code, status))
            cert = self._certificate(answer)
            if cert['serial'] != state['certificate']['serial']:
                state['previousCertificate'] = state['certificate']
            state['certificate'] = cert
            protocols = _protocols(answer)
            if protocols is not None:
                state['protocols'] = protocols
            state['rotate'] = False
            state.pop('pendingRotation', None)
            self._save(state)
            self.journal('rotated', serial=cert['serial'], notAfter=cert['notAfter'])
            return self.describe()
    def rotation_due(self, state: dict) -> bool:
        cert = state['certificate']
        start, end = strict_time(cert['notBefore']) / 1000, strict_time(cert['notAfter']) / 1000
        return bool(state.get('rotate')) or self.clock() >= start + (end - start) * 2 / 3

    def expired(self, state: dict) -> bool:
        """On the server's clock, as the last check-in measured it: the clock every request is signed on."""
        return self.clock() + (state.get('skewSeconds') or 0) >= strict_time(state['certificate']['notAfter']) / 1000

    def _require(self, renew: bool = False, force: bool = False) -> dict:
        """The saved state, or a refusal that names what happened and the person's next step (OBS-199, OBS-204):
        a removed or expired enrollment never reaches the network. With `renew` (a check-in, a tick), an expired
        certificate inside its renewal window is renewed by the key on record first (DEC-0100); `force` (a person's
        check-in) asks now even while an unanswered renewal backs off."""
        state = self.state()
        if state is None:
            raise OrgError('not-enrolled', 'this machine is not enrolled in that organization')
        if isinstance(state.get('removed'), dict):
            raise self._removed_error(state)
        if renew and renewal(state, self.clock()) is not None:
            self._renew(state, force=force)
            return self.state()
        if self.expired(state):
            raise self._expired_error(state)
        return state

    def _expired_error(self, state: dict, detail: str = '') -> 'OrgError':
        said = notice(self.org_id, state, self.clock())
        renews = 'renewUntil' in said
        return OrgError('certificate-expired', 'the certificate expired on %s%s: %s'
                        % (state['certificate']['notAfter'], detail,
                           'it renews by its key on record until %s' % said['renewUntil'] if renews
                           else 'this machine needs a new enrollment token'),
                        next_step=said.get('nextStep'), command=said.get('command'))

    def _renew(self, state: dict, force: bool = False) -> dict:
        """Renewal after expiry by the key on record (contract DEC-0100): the enrollment request with
        `auth.method: "key-on-record"`, a CSR from this enrollment's `#sig` key, no token and no `Authorization` header,
        `requestedAt` on the server's clock. Taken as any re-enrollment (a new certificate and epoch) with the binding
        on record kept. Under the lock, which the callers hold. An unanswered renewal (no answer, 408, 425, 429, 5xx)
        backs off and is asked again; a refusal is recorded for this certificate and not asked again, and the machine
        then needs a new token; a `certificate-revoked` or `certificate-unknown` with 401 removes it (OBS-204)."""
        with self._locked():
            window = renewal(state, self.clock())
            if window is None:
                raise self._expired_error(state)
            check_in = 'project-observatory full org check-in --org %s' % self.org_id
            if not force and window['nextAt'] > self.clock():
                raise OrgError('renewal-pending', 'the renewal by the key on record waits for its backoff until %s'
                               % iso(window['nextAt']), next_step=notice(self.org_id, state, self.clock())['nextStep'],
                               command=check_in)
            serial = state['certificate']['serial']
            expired = self.expired(state)
            request = self.enrollment_request(None, at=self.clock() + (state.get('skewSeconds') or 0))
            status, answer = self._post(state['server'], self.paths['enroll'], request, {})
            if status == 200 and isinstance(answer, dict) and answer.get('kind') == 'enrollment-response':
                try:
                    self._accept(state['server'], answer, state, False, renewed={'expired': expired})
                except OrgError as error:
                    self._renewal_refused(state, serial, status, error.code)
                    raise self._expired_error(self.state(), ' and its renewal was refused (%s)' % error.code) \
                        from None
                self._register_quietly()
                return window
            code = _error_code(answer) or ('unreachable' if status == 0 else 'http-%d' % status)
            self.journal('renewal-refused', status=status, code=code)
            if status == 401 and code in REMOVED_CODES:         # a revoked or forgotten device never renews
                self._remove(state, code, status)
                raise self._removed_error(state)
            last = {'at': iso(self.clock()), 'status': status, 'code': code}
            if 400 <= status < 500 and status not in RENEWAL_RETRY_STATUSES:
                self._renewal_refused(state, serial, status, code)
                raise self._expired_error(self.state(), ' and the server refused its renewal by the key on record '
                                          '(%s, HTTP %s)' % (code, status))
            attempts = window['attempts'] + 1
            state['renewal'] = {'serial': serial, 'attempts': attempts, 'nextAt': self._backoff(attempts - 1),
                                'last': last}
            self._save(state)
            raise OrgError('renewal-failed', 'the renewal by the key on record went unanswered (%s, HTTP %s); it is '
                           'asked again from %s' % (code, status, iso(state['renewal']['nextAt'])),
                           next_step=notice(self.org_id, state, self.clock())['nextStep'], command=check_in)

    def _renewal_refused(self, state: dict, serial: str, status: int, code: str) -> None:
        """The server decided: no renewal for this certificate, and no request asks it again (a new token resets it)."""
        state['renewal'] = {'serial': serial, 'refused': True,
                            'last': {'at': iso(self.clock()), 'status': status, 'code': code}}
        self._save(state)

    # -- check-in ----------------------------------------------------------------------------------------------
    def check_in_document(self, state: dict) -> dict:
        from configuration import VERSION
        with self._db() as conn:
            last = conn.execute('SELECT MAX(seq) AS s FROM events WHERE epoch = ?', (state['epoch'],)).fetchone()['s'] or 0
            floor = conn.execute("SELECT MIN(seq) AS s FROM events WHERE epoch = ? AND state = 'pending'",
                                 (state['epoch'],)).fetchone()['s']
            dropped = self._undelivered_drops(conn, state['epoch'])
            retrying = (self._meta(conn, 'retry') or {}).get('attempts', 0) > 0
        healthy = not retrying and not dropped
        health = {'state': 'healthy'} if healthy else \
            {'state': 'degraded', 'detail': 'activity outbox retrying' if retrying else 'activity events dropped'}
        doc = {'protocol': DEVICE_PROTOCOL, 'kind': 'check-in', 'protocols': list(NODE_READS),
               'device': {'id': self.keys.device_id}, 'sequenceNum': state['sequenceNum'] + 1, 'at': iso(self.clock()),
               'agent': {'version': VERSION, 'buildChannel': 'official'},
               'health': health, 'logging': {'enabled': True},
               'collectors': [{'collector': {'id': COLLECTOR_ID, 'epoch': state['epoch']}, 'source': 'meter',
                               'lastSeq': last, 'bufferedFrom': floor if floor is not None else last + 1,
                               'dropped': dropped, 'healthy': healthy}],
               'policy': {'layers': policy_report(state)},
               'certificate': {'serial': state['certificate']['serial'], 'notAfter': state['certificate']['notAfter']}}
        if state.get('fullState'):
            doc['fullState'] = True
        return doc
    def check_in(self, force_renewal: bool = True, inputs: Optional[dict] = None) -> dict:
        """One signed check-in. An expired certificate inside its renewal window is renewed by the key on record
        first (DEC-0100); the answer then names it (`renewed`). A person's check-in asks the renewal now
        (`force_renewal`); the tick respects its backoff.

        A due harness summary rides in it (`records.nodeSummary`). A check-in the server refuses in a way the summary
        may cause (`SUMMARY_SUSPECT_STATUSES`, or a refusal naming the summary) is sent again at once without it; when
        that one is taken, the refusal is recorded (`summaryRefused`) and the summary waits
        `SUMMARY_BACKOFF_SECONDS`, so a server that will not read it never stops check-ins, rotation or renewal."""
        if inputs is None:
            current = self.state() or {}
            inputs = self.summary_inputs() if self._summary_wanted(current) else None   # outside the lock
        with self._locked():
            before = self.state()
            state = self._require(renew=True, force=force_renewal)
            renewed = {'by': 'key-on-record', 'epoch': state['epoch'], 'serial': state['certificate']['serial'],
                       'notAfter': state['certificate']['notAfter']} \
                if before is not None and before.get('epoch') != state.get('epoch') else None
            doc = self.check_in_document(state)
            state['sequenceNum'] = doc['sequenceNum']      # a sequence number is spent once sent, answered or not
            pending = self._summary_due(state, inputs)
            if pending is not None:                        # DEC-0101: a check-in record, inside the signed request
                doc['records'] = {'nodeSummary': pending['summary']}
                # DEC-0114: the summary is read with the check-in and judged against this enrollment's certificate (its
                # device and `org:<org.id>`, as written) before it leaves; one that would be refused stays here.
                findings = node_summary.check_in_summary_findings(doc) + node_summary.check_in_attribution_findings(
                    doc, {'org': {'id': self.org_id}, 'device': {'id': state.get('device')}})
                if findings:
                    del doc['records']
                    state['nodeSummaryNote'] = {'at': iso(self.clock()), 'reason': 'attribution',
                                                'detail': '; '.join(sorted({f['instancePath'] for f in findings}))}
                    self.journal('node-summary-not-built', code='attribution')
                    pending = None
            if pending is not None:
                state['summarySequence'] = pending['summary']['sequence']   # spent once sent, as sequenceNum
            self._save(state)
            def resend(sent: dict):
                def again(current: dict) -> dict:          # resent under the previous certificate: it names that one
                    current['sequenceNum'] += 1
                    self._save(current)
                    return dict(sent, sequenceNum=current['sequenceNum'],
                                certificate={'serial': current['certificate']['serial'],
                                             'notAfter': current['certificate']['notAfter']})
                return again
            status, answer = self._signed(state, self.paths['check-in'], doc, resend(doc))
            removed = isinstance(state.get('removed'), dict)   # DEC-0103 or OBS-204: nothing is sent again
            if pending is not None and not removed and _summary_suspect(status, answer):
                code, first_status = _error_code(answer) or 'http-%d' % status, status
                state['sequenceNum'] += 1                  # the same check-in, without the summary, signed afresh
                bare = dict({k: v for k, v in doc.items() if k != 'records'}, sequenceNum=state['sequenceNum'])
                self._save(state)
                status, answer = self._signed(state, self.paths['check-in'], bare, resend(bare))
                if status != 200 or not isinstance(answer, dict) or answer.get('kind') != 'check-in-response':
                    # Refused without it too: the summary is not to blame, and it waits for the next check-in that
                    # is taken, so a refusing server gets one request per check-in, not two.
                    state['summaryHeld'] = True
                else:
                    retry = int(self.clock()) + SUMMARY_BACKOFF_SECONDS
                    state['summaryRefused'] = {'at': iso(self.clock()), 'status': first_status,
                                               'code': code, 'sequence': pending['summary']['sequence'],
                                               'retryAt': iso(retry)}
                    state['summaryBackoffUntil'] = retry
                    self.journal('node-summary-refused', code=code)
                    pending = None
            if status != 200 or not isinstance(answer, dict) or answer.get('kind') != 'check-in-response':
                code = _error_code(answer) or ('unreachable' if status == 0 else 'http-%d' % status)
                state['lastCheckIn'] = {'at': iso(self.clock()), 'result': code}
                state['nextCheckInAt'] = 0 if code == 'signature-stale' else \
                    int(self.clock()) + min(state['checkInSeconds'], 300)
                ended = _ends(status, code) or isinstance(state.get('removed'), dict)
                if ended:
                    state['reEnroll'], state['reEnrollReason'] = True, code
                if pending is not None:
                    state['nodeSummaryNote'] = {'at': iso(self.clock()), 'reason': 'check-in-failed', 'code': code}
                self._save(state)
                self.journal('check-in-failed', status=status, code=code)
                out = {'ok': False, 'code': code, 'status': status}
                if renewed:
                    out['renewed'] = renewed
                if ended:                                 # never a silent refusal: what happened and the step
                    said = notice(self.org_id, state, self.clock())
                    out.update({k: said[k] for k in ('removed', 'nextStep', 'command') if k in said})
                return out
            flags = answer.get('flags') if isinstance(answer.get('flags'), list) else []
            state['fullState'] = 'report_full_state' in flags
            nxt = answer.get('nextCheckInSeconds')
            state['checkInSeconds'] = nxt if isinstance(nxt, int) and 30 <= nxt <= 86400 else state['checkInSeconds']
            state['nextCheckInAt'] = int(self.clock()) + state['checkInSeconds']
            if strict_time(answer.get('collectionStart')) is not None:
                state['collectionStart'] = answer['collectionStart']
            arrived = _policy_arrived(state, answer.get('policy'), self.clock(), self.org_id)
            if arrived is not None:                        # OBS-231: held, never applied, until OE-06
                self.journal('policy-not-applied', source=arrived['source'], revision=arrived['revision'])
            # DEC-0102: the notice moved when a response names another acknowledged version (absent is no signal) or the
            # applied policy another `noticeVersion` (OE-06 applies one; until then `notice_version` is None).
            acknowledged, version = answer.get('acknowledgedNoticeVersion'), self.notice_version()
            notice_moved = False
            if isinstance(acknowledged, str) and NOTICE_VERSION.fullmatch(acknowledged) \
                    and acknowledged != state.get('acknowledgedNoticeVersion'):
                state['acknowledgedNoticeVersion'], notice_moved = acknowledged, True
            if version is not None and version != state.get('noticeVersion'):
                state['noticeVersion'], notice_moved = version, True
            if notice_moved:
                self._release_purpose_holds(state)
            protocols = _protocols(answer)                 # DEC-0094: the held list is replaced from wherever it comes
            listed_before = self.accepts_shares(state)
            if protocols is not None:
                state['protocols'] = protocols
            action = (answer.get('certificate') or {}).get('action') if isinstance(answer.get('certificate'), dict) \
                else None
            state['rotate'] = state.get('rotate') or action == 'rotate'
            state['reEnroll'] = action == 're-enroll'
            state.pop('reEnrollReason', None)
            state.pop('unknownOrg', None)                  # the provisional rule's record, before DEC-0103
            server_at = strict_time(answer.get('at'))
            skew = None if server_at is None else round(server_at / 1000 - self.clock())
            if skew is not None and abs(skew) <= 86400:
                state['skewSeconds'] = skew
            state['lastCheckIn'] = {'at': iso(self.clock()), 'result': 'ok', 'skewSeconds': skew}
            if pending is not None:
                state['nodeSummary'] = {'sequence': pending['summary']['sequence'], 'sentAt': iso(self.clock()),
                                        'digest': pending['digest'], 'counts': node_summary.counts(pending['summary']),
                                        'omitted': pending['omitted'], 'drift': pending['summary'].get('drift')}
                state.pop('nodeSummaryNote', None)
                state.pop('summaryRefused', None)
                state.pop('summaryBackoffUntil', None)
            state.pop('summaryHeld', None)                 # a check-in was taken: a held summary may ride again
            self._save(state)
            self.journal('checked-in', sequenceNum=state['sequenceNum'], action=action, flags=flags,
                         **({'nodeSummary': pending['summary']['sequence']} if pending is not None else {}))
            newly_listed = self.accepts_shares(state) and not listed_before
            if notice_moved:                               # DEC-0102: a notice refusal is sent again on a new notice
                with self._db() as conn:
                    conn.execute("UPDATE registrations SET sent_at = NULL, next_at = 0 WHERE gate = 'hold-notice'")
        if newly_listed or notice_moved:                   # the server began to take registrations, or the notice
            self._register_quietly()                       # moved: send them now
        out = {'ok': True, 'nextCheckInSeconds': state['checkInSeconds'], 'action': action}
        if renewed:
            out['renewed'] = renewed
        return out
    # -- the outbox --------------------------------------------------------------------------------------------
    def enqueue(self, kind: str, data: dict, *, occurred: str, key: Optional[str] = None, source: str = 'meter',
                monotonic: str = '0') -> Optional[dict]:
        """Persists one event of this enrollment's stream once (`key` dedupes), with its id and the next seq. An event
        of a kind the server rejected for its purpose is not collected while that hold stands (DEC-0100): None, as for
        a duplicate."""
        with self._locked():
            state = self._require()
            if kind in self.purpose_holds(state):
                return None
            with self._db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    if key and conn.execute('SELECT 1 FROM events WHERE key = ?', (key,)).fetchone():
                        conn.execute('COMMIT')
                        return None
                    event = self._insert(conn, state['epoch'], kind, data, occurred=occurred, key=key, source=source,
                                         monotonic=monotonic)
                    self._bound(conn, state)
                    conn.execute('COMMIT')
                except BaseException:
                    conn.execute('ROLLBACK')
                    raise
            return event

    def _insert(self, conn, epoch: int, kind: str, data: dict, *, occurred: str, key: Optional[str] = None,
                source: str = 'meter', monotonic: str = '0') -> dict:
        seq = (conn.execute('SELECT MAX(seq) AS s FROM events WHERE epoch = ?', (epoch,)).fetchone()['s'] or 0) + 1
        event = {'protocol': ACTIVITY_PROTOCOL, 'eventId': ulid(int(self.clock() * 1000)), 'kind': kind,
                 'source': source, 'collector': {'id': COLLECTOR_ID, 'epoch': epoch}, 'seq': seq,
                 'occurred': occurred, 'bootId': _BOOT, 'monotonicNs': monotonic, 'data': data}
        conn.execute('INSERT INTO events(epoch, seq, event_id, kind, key, body) VALUES(?, ?, ?, ?, ?, ?)',
                     (epoch, seq, event['eventId'], kind, key, json.dumps(event, sort_keys=True)))
        return event
    def _bound(self, conn, state: dict) -> None:
        """Past MAX_PENDING the oldest pending events are dropped down to OVERFLOW_HEADROOM under the bound, so a burst
        ends in one `buffer.overflow` (activity.md, streams). It never renumbers."""
        pending = conn.execute("SELECT COUNT(*) AS n FROM events WHERE state = 'pending'").fetchone()['n']
        if pending <= MAX_PENDING:
            return
        rows = conn.execute("SELECT seq, kind FROM events WHERE state = 'pending' AND epoch = ? AND kind != "
                            "'buffer.overflow' ORDER BY seq LIMIT ?",
                            (state['epoch'], pending - (MAX_PENDING - OVERFLOW_HEADROOM))).fetchall()
        self._drop(conn, state['epoch'], rows)

    def _drop(self, conn, epoch: int, rows: List[sqlite3.Row]) -> None:
        """Consumes `rows` (seq, kind) as dropped and names them in one `buffer.overflow`: the newest one not yet sent
        takes the new ranges; otherwise a new one. A dropped event's key is freed, so a `grant.usage` day is enqueued
        again once it may be sent."""
        if not rows:
            return
        seqs = sorted(r['seq'] for r in rows)
        conn.executemany("UPDATE events SET state = 'dropped', key = NULL WHERE epoch = ? AND seq = ?",
                         [(epoch, q) for q in seqs])
        ranges: List[dict] = []
        for q in seqs:
            if ranges and ranges[-1]['toSeq'] + 1 == q:
                ranges[-1]['toSeq'] = q
            else:
                ranges.append({'fromSeq': q, 'toSeq': q})
        by_kind: Dict[str, int] = {}
        for row in rows:
            by_kind[row['kind']] = by_kind.get(row['kind'], 0) + 1
        inflight = self._meta(conn, 'inflight') or {}
        span = inflight.get('span') or [None, 0, -1]
        target = conn.execute("SELECT seq, body FROM events WHERE epoch = ? AND kind = 'buffer.overflow' AND state = "
                              "'pending' ORDER BY seq DESC LIMIT 1", (epoch,)).fetchone()
        if target is not None and not (span[0] == epoch and span[1] <= target['seq'] <= span[2]):
            event = json.loads(target['body'])
            held = event['data']['dropped']
            merged = sorted(held['ranges'] + ranges, key=lambda r: r['fromSeq'])
            held['ranges'] = []
            for r in merged:
                if held['ranges'] and held['ranges'][-1]['toSeq'] + 1 >= r['fromSeq']:
                    held['ranges'][-1]['toSeq'] = max(held['ranges'][-1]['toSeq'], r['toSeq'])
                else:
                    held['ranges'].append(dict(r))
            held['events'] += len(rows)
            kinds = {k['kind']: k['count'] for k in held['byKind']}
            for k, n in by_kind.items():
                kinds[k] = kinds.get(k, 0) + n
            held['byKind'] = [{'kind': k, 'count': n} for k, n in sorted(kinds.items())]
            conn.execute('UPDATE events SET body = ? WHERE epoch = ? AND seq = ?',
                         (json.dumps(event, sort_keys=True), epoch, target['seq']))
            return
        self._insert(conn, epoch, 'buffer.overflow',
                     {'dropped': {'events': len(rows), 'ranges': ranges,
                                  'byKind': [{'kind': k, 'count': n} for k, n in sorted(by_kind.items())]}},
                     occurred=iso(self.clock()), monotonic=str(time.monotonic_ns()))

    def _undelivered_drops(self, conn, epoch: int) -> List[dict]:
        """The dropped ranges whose `buffer.overflow` the server has not taken yet: what a check-in reports."""
        out: List[dict] = []
        for row in conn.execute("SELECT body FROM events WHERE epoch = ? AND kind = 'buffer.overflow' AND state = "
                                "'pending' ORDER BY seq", (epoch,)):
            out.extend({'fromSeq': r['fromSeq'], 'toSeq': r['toSeq']}
                       for r in json.loads(row['body'])['data']['dropped']['ranges'])
        return out
    def _slice(self, conn, limit: int = MAX_BATCH) -> Tuple[Optional[int], List[sqlite3.Row]]:
        """The next batch: the oldest stream with pending events, its consecutive run from the oldest, at most
        `limit`."""
        head = conn.execute("SELECT epoch, seq FROM events WHERE state = 'pending' ORDER BY epoch, seq LIMIT 1").fetchone()
        if head is None:
            return None, []
        rows = conn.execute("SELECT seq, event_id, kind, body FROM events WHERE state = 'pending' AND epoch = ? AND "
                            "seq >= ? ORDER BY seq LIMIT ?", (head['epoch'], head['seq'], limit)).fetchall()
        run = []
        for row in rows:
            if run and row['seq'] != run[-1]['seq'] + 1:
                break
            run.append(row)
        return head['epoch'], run
    def flush(self, max_batches: int = 20) -> dict:
        """Sends pending events, batch by batch, until the outbox is empty, a batch fails, or the backoff holds.

        A `grant.usage` whose grant's registration answer no longer lets it (`gate` is checked again at send) ends the
        batch before it; at the head of the stream it is dropped and named in a `buffer.overflow`, and the day is
        enqueued again once the gate opens. A batch refused for what it is (400, 422, `device-mismatch`) is consumed
        as rejected; a 413 halves the batch size first. A 200 that acknowledges none of the batch is held and retried
        under the same `batchId`."""
        with self._locked():
            state = self._require()
            sent = {'batches': 0, 'accepted': 0, 'duplicates': 0, 'rejected': 0}
            if self._release_purpose_holds(state):
                self._save(state)
            held_kinds = self.purpose_holds(state)
            with self._db() as conn:
                retry = self._meta(conn, 'retry', {'attempts': 0, 'nextAt': 0})
                if retry['nextAt'] > self.clock():
                    return dict(sent, held='backoff', nextAttempt=iso(retry['nextAt']))
                gates = {r['grant_id']: r['gate'] for r in conn.execute('SELECT grant_id, gate FROM registrations')}
                for _ in range(max_batches * 4):
                    if sent['batches'] >= max_batches:
                        break
                    limit = self._meta(conn, 'batchLimit', MAX_BATCH)
                    epoch, run = self._slice(conn, limit)
                    if not run:
                        break
                    cut = next((i for i, r in enumerate(run) if r['kind'] in held_kinds or (
                        r['kind'] == 'grant.usage'
                        and gates.get(json.loads(r['body'])['data'].get('grantId')) != 'send')), None)
                    if cut == 0:
                        self._drop(conn, epoch, run[:1])
                        self.journal('purpose-withheld' if run[0]['kind'] in held_kinds else 'grant-usage-withheld',
                                     seq=run[0]['seq'])
                        continue
                    run = run[:cut] if cut else run
                    inflight = self._meta(conn, 'inflight')
                    span = [epoch, run[0]['seq'], run[-1]['seq']]
                    batch_id = inflight['batchId'] if inflight and inflight.get('span') == span else ulid()
                    self._set_meta(conn, 'inflight', {'batchId': batch_id, 'span': span})
                    doc = {'protocol': ACTIVITY_PROTOCOL, 'device': {'id': self.keys.device_id}, 'batchId': batch_id,
                           'sent': iso(self.clock()), 'events': [json.loads(r['body']) for r in run]}
                    status, ack = self._signed(state, self.paths['batches'], doc)
                    if isinstance(state.get('removed'), dict):  # nothing to retry: the organization is gone
                        return dict(sent, held='removed', removed=state['removed'])
                    code = None if status == 200 and isinstance(ack, dict) else \
                        _error_code(ack) or ('unreachable' if status == 0 else 'http-%d' % status)
                    # A refusal that names the organization or the certificate is not about the batch's content: it
                    # is held and retried, never rejected, whatever its status (a 401 removal and DEC-0103's 410,
                    # the enrollment's end and not a rejection of its events, returned above).
                    about_us = code in REMOVED_CODES or code in (UNKNOWN_ORG, ORGANIZATION_CLOSED)
                    if status == 413 and len(run) > 1 and not about_us:
                        self._set_meta(conn, 'batchLimit', max(1, len(run) // 2))
                        self._set_meta(conn, 'inflight', None)
                        self.journal('batch-too-large', events=len(run), batchId=batch_id)
                        continue
                    if code is not None and not about_us and (status in PERMANENT_BATCH_STATUSES
                                                              or code in PERMANENT_BATCH_CODES):
                        conn.executemany("UPDATE events SET state = 'rejected', reason = ? WHERE epoch = ? AND seq = ?",
                                         [(code[:40], epoch, r['seq']) for r in run])
                        self._set_meta(conn, 'inflight', None)
                        self._set_meta(conn, 'lastBatch', {'at': iso(self.clock()), 'result': code, 'status': status,
                                                           'events': len(run), 'permanent': True})
                        self.journal('batch-refused', status=status, code=code, events=len(run), batchId=batch_id)
                        sent['rejected'] += len(run)
                        continue
                    if code is None:
                        counts = self._consume(conn, epoch, run, ack)
                        purpose = counts.pop('purposeKinds')
                        if purpose:                        # DEC-0100: that kind waits for the notice to move
                            held_kinds = self._hold_purpose(state, purpose)
                        left = conn.execute("SELECT COUNT(*) AS n FROM events WHERE epoch = ? AND seq BETWEEN ? AND ? "
                                            "AND state = 'pending'", (epoch, span[1], span[2])).fetchone()['n']
                        if left == len(run):
                            code = 'not-acknowledged'
                    if code is not None:
                        retry = {'attempts': retry['attempts'] + 1, 'nextAt': self._backoff(retry['attempts'])}
                        self._set_meta(conn, 'retry', retry)
                        self._set_meta(conn, 'lastBatch', {'at': iso(self.clock()), 'result': code, 'status': status,
                                                           'events': len(run)})
                        self.journal('batch-failed', status=status, code=code, events=len(run), batchId=batch_id)
                        return dict(sent, held=code, nextAttempt=iso(retry['nextAt']))
                    retry = {'attempts': 0, 'nextAt': 0}
                    self._set_meta(conn, 'retry', retry)
                    self._set_meta(conn, 'inflight', None)
                    self._set_meta(conn, 'batchLimit', min(MAX_BATCH, limit * 2))
                    self._set_meta(conn, 'lastBatch', {'at': iso(self.clock()), 'result': 'ok', 'status': 200,
                                                       'events': len(run), **counts})
                    self.journal('batch-sent', batchId=batch_id, events=len(run), **counts)
                    sent['batches'] += 1
                    for name in ('accepted', 'duplicates', 'rejected'):
                        sent[name] += counts[name]
            return sent
    def _consume(self, conn, epoch: int, run: List[sqlite3.Row], ack: dict) -> dict:
        """Applies an ack: rejected events are consumed with their reason, whatever it is (a reason this node does not
        know included, DEC-0100); every seq up to the stream's ack, and in its `held` ranges, is consumed. What the ack
        does not name stays pending and goes in a later batch. The kinds of this run's events rejected for their
        purpose come back in `purposeKinds` (`before-purpose` holds nothing, DEC-0102)."""
        rejected = [r for r in ack.get('rejected', []) if isinstance(r, dict)] if isinstance(ack.get('rejected'), list) else []
        kinds = {r['event_id']: r['kind'] for r in run}
        purpose_kinds: List[str] = []
        for item in rejected:
            conn.execute("UPDATE events SET state = 'rejected', reason = ? WHERE event_id = ? AND state = 'pending'",
                         (str(item.get('reason'))[:40], item.get('eventId')))
            kind = kinds.get(item.get('eventId')) if isinstance(item.get('eventId'), str) else None
            if item.get('reason') == PURPOSE and kind is not None and kind not in purpose_kinds:
                purpose_kinds.append(kind)
        for entry in ack.get('acks', []) if isinstance(ack.get('acks'), list) else []:
            collector = entry.get('collector') if isinstance(entry, dict) else None
            if not isinstance(collector, dict) or collector.get('id') != COLLECTOR_ID:
                continue
            stream = collector.get('epoch')
            seq = entry.get('seq')
            if isinstance(seq, int):
                conn.execute("UPDATE events SET state = 'acked' WHERE epoch = ? AND seq <= ? AND state = 'pending'",
                             (stream, seq))
            for held in entry.get('held', []) if isinstance(entry.get('held'), list) else []:
                if isinstance(held, dict) and isinstance(held.get('fromSeq'), int) and isinstance(held.get('toSeq'), int):
                    conn.execute("UPDATE events SET state = 'acked' WHERE epoch = ? AND seq BETWEEN ? AND ? AND "
                                 "state = 'pending'", (stream, held['fromSeq'], held['toSeq']))
        num = lambda name: ack.get(name) if isinstance(ack.get(name), int) else 0
        return {'accepted': num('accepted'), 'duplicates': num('duplicates'), 'rejected': len(rejected),
                'purposeKinds': purpose_kinds}

    # -- a purpose rejection (DEC-0100, DEC-0102) --------------------------------------------------------------
    def notice_version(self) -> Optional[str]:
        """The `noticeVersion` of the signed policy this machine applied for this organization (its `mdm` or `server`
        layer, in that precedence), or None. None until the step that verifies and applies the server's signed policy
        lands (OE-06, `policy_layers`)."""
        for source in ('mdm', 'server'):
            for layer in self.policy_layers():
                version = layer.get('noticeVersion') if isinstance(layer, dict) and layer.get('source') == source \
                    else None
                if isinstance(version, str) and NOTICE_VERSION.fullmatch(version):
                    return version
        return None

    @staticmethod
    def purpose_holds(state: dict) -> Dict[str, dict]:
        """The event kinds held after a `purpose` rejection: `{kind: {at, noticeVersion, acknowledged}}`, the two
        versions as they stood at the rejection (None: absent)."""
        holds = state.get('purposeHolds')
        return {k: v for k, v in holds.items() if isinstance(v, dict)} if isinstance(holds, dict) else {}

    @staticmethod
    def purpose_holdable(state: dict) -> bool:
        """Whether a release signal can reach a hold on this enrollment: it binds a person (not `org-cloud`, not an
        unassigned device) and has received that person's `acknowledgedNoticeVersion` at least once. Until OE-06
        applies the signed policy, its `noticeVersion` is no signal this node can read, so a hold on any other
        enrollment would drop that kind for good (an `org-cloud` machine, or a managed one collecting under the mdm
        deadline before any acknowledgement)."""
        cert = state.get('certificate') if isinstance(state.get('certificate'), dict) else {}
        return cert.get('user') is True and cert.get('role') != 'org-cloud' \
            and isinstance(state.get('acknowledgedNoticeVersion'), str)

    def _hold_purpose(self, state: dict, kinds: List[str]) -> Dict[str, dict]:
        """Records a hold per kind the receiver rejected for its purpose, with the notice versions held now, and saves
        the state; a kind already held keeps the versions of its first rejection. Where no release signal can arrive
        (`purpose_holdable`), and for `buffer.overflow` (the stream's own record: holding it would make the next drop
        write another), the rejection is consumed like `before-purpose`: the kind is still collected and sent, the
        server refuses it until its notice covers it, and the refusal is logged once per kind per UTC day."""
        holds = self.purpose_holds(state)
        holdable = self.purpose_holdable(state)
        logged = state.get('purposeRefused') if isinstance(state.get('purposeRefused'), dict) else {}
        today = iso(self.clock())[:10]
        for kind in kinds:
            if not holdable or kind == 'buffer.overflow':
                if logged.get(kind) != today:
                    logged[kind] = today
                    self.journal('purpose-refused', kind=kind, held=False)
                continue
            if kind not in holds:
                holds[kind] = {'at': iso(self.clock()), 'noticeVersion': self.notice_version(),
                               'acknowledged': state.get('acknowledgedNoticeVersion')}
                self.journal('purpose-held', kind=kind)
        if logged:
            state['purposeRefused'] = logged
        if holds:
            state['purposeHolds'] = holds
        self._save(state)
        return holds

    def _release_purpose_holds(self, state: dict) -> List[str]:
        """DEC-0102: a held kind is sent again only when the applied policy names another `noticeVersion` than at the
        rejection, or a check-in response named an `acknowledgedNoticeVersion` other than the one held at the
        rejection (absence at the rejection included: a first acknowledgement is a change). A value that becomes
        absent is no signal, and nothing is released on a timer. Returns the kinds released; the caller saves."""
        holds = self.purpose_holds(state)
        if not holds:
            return []
        version, acknowledged = self.notice_version(), state.get('acknowledgedNoticeVersion')
        released = [kind for kind, hold in holds.items()
                    if (version is not None and version != hold.get('noticeVersion'))
                    or (acknowledged is not None and acknowledged != hold.get('acknowledged'))]
        for kind in released:
            del holds[kind]
            self.journal('purpose-released', kind=kind)
        if holds:
            state['purposeHolds'] = holds
        else:
            state.pop('purposeHolds', None)
        return released
    # -- share registrations and grant usage -------------------------------------------------------------------
    def accepts_shares(self, state: dict) -> bool:
        return SHARE_PROTOCOL in (state.get('protocols') or [])

    # -- the node summary (DEC-0101, OBS-192) ------------------------------------------------------------------
    def accepts_summary(self, state: dict) -> bool:
        """A summary goes only to a server that lists `node-summary/1` in its protocols (DEC-0067, DEC-0094)."""
        return SUMMARY_PROTOCOL in (state.get('protocols') or [])

    def policy_layers(self) -> List[dict]:
        """The signed policy layers this machine verified and applied for this organization, which the baseline is read
        from. None yet: verifying and applying the server's signed policy and its key sets is OE-06 (the check-in
        reports server revision 0), so the summary carries no drift until then, as the contract asks of a summary
        under a policy without a baseline."""
        return []

    def services_in_context(self) -> List[dict]:
        """The agent services placed in this organization's context, and no other. None yet: a Fabric service
        descriptor names no context and no agent reference an estate placement could be matched to, so no service on
        this machine can be shown to be placed in `org:<org.id>`; listing a person's other services would cross
        contexts (DEC-0082), so the list stays empty rather than guessed."""
        return []

    def consents(self, state: dict) -> bool:
        """Whether the person agreed to share this machine's harness with this organization (`org summary --consent`).
        Off until they do: a summary names every agent, skill and plugin here, whatever the context it serves, and the
        contract's question about other contexts (CT-92) is still open."""
        consent = state.get('summaryConsent')
        return isinstance(consent, dict) and consent.get('on') is True

    def set_summary_consent(self, on: bool) -> dict:
        """Turns sharing the harness summary with this organization on or off, recorded with its time."""
        with self._locked():
            state = self.state()
            if state is None:
                raise OrgError('not-enrolled', 'this machine is not enrolled in organization %s' % self.org_id,
                               command=enroll_command(self.org_id, None))
            state['summaryConsent'] = {'on': bool(on), 'at': iso(self.clock())}
            if on:
                state.pop('summaryBackoffUntil', None)     # a fresh consent sends at the next check-in
            self._save(state)
            self.journal('node-summary-consent', on=bool(on))
            return {'org': self.org_id, 'consent': state['summaryConsent'], 'nodeSummary': summary_notice(state)}

    def summary_inputs(self) -> dict:
        """What a summary is built from, read without the enrollment's lock (the scans can be large): the two scans
        or why they cannot be used, and the person's exclusion list or why it cannot be read."""
        harness_scan, mcp_scan, missing = self.harness()
        exclude, unreadable = self.exclusions()
        return {'harness': harness_scan, 'mcp': mcp_scan, 'missing': missing, 'exclude': exclude,
                'exclusionsUnreadable': unreadable}

    def summary_for(self, state: dict, sequence: int, inputs: Optional[dict] = None) -> dict:
        """The summary this enrollment would send now with `sequence`: `{summary, digest, omitted, excluded}`. Raises
        OrgError `node-summary-unavailable` when the scans or the exclusion list cannot be used, and
        `node-summary-refused` when what they hold would break FAC-SEM-128."""
        inputs = self.summary_inputs() if inputs is None else inputs
        if inputs.get('missing'):
            raise OrgError('node-summary-unavailable', inputs['missing'],
                           next_step='turn on the harness and MCP scans (integrations.harness, integrations.mcp) and '
                                     'let the next tick take them', command='project-observatory full doctor')
        if inputs.get('exclusionsUnreadable'):
            raise OrgError('node-summary-unavailable', inputs['exclusionsUnreadable'],
                           next_step='fix or remove the exclusion list; nothing is sent until it can be read',
                           command='project-observatory full org summary --org %s' % self.org_id)
        baseline = node_summary.harness_baseline(self.policy_layers())
        try:
            summary, omitted, excluded = node_summary.build(
                device_id=self.keys.device_id, org_id=self.org_id, sequence=sequence, at=iso(self.clock()),
                harness_scan=inputs.get('harness'), mcp_scan=inputs.get('mcp'), services=self.services_in_context(),
                baseline=baseline, exclude=inputs.get('exclude') or ())
        except node_summary.SummaryRefused as error:
            raise OrgError('node-summary-refused', str(error)) from None
        return {'summary': summary, 'digest': node_summary.content_digest(summary), 'omitted': omitted,
                'excluded': excluded}

    def _summary_wanted(self, state: dict) -> bool:
        """Whether a summary may go in this check-in at all: the server reads it, the person consented, and no refusal
        by the server is still being waited out."""
        backoff = state.get('summaryBackoffUntil')
        return self.accepts_summary(state) and self.consents(state) and not state.get('summaryHeld') and \
            not (isinstance(backoff, (int, float)) and self.clock() < backoff)

    def _summary_due(self, state: dict, inputs: Optional[dict] = None) -> Optional[dict]:
        """The summary to put in this check-in, or None: none when the server does not read `node-summary/1`, the
        person has not consented, the server's last refusal is being waited out, it cannot be built (named in
        `nodeSummaryNote`), or the last one the server took says the same and is less than a day old."""
        if not self._summary_wanted(state):
            return None
        try:
            pending = self.summary_for(state, (state.get('summarySequence') or 0) + 1, inputs)
        except OrgError as error:
            state['nodeSummaryNote'] = {'at': iso(self.clock()), 'reason': error.code, 'detail': str(error)[:300]}
            self.journal('node-summary-not-built', code=error.code)
            return None
        state.pop('nodeSummaryNote', None)
        last = state.get('nodeSummary') if isinstance(state.get('nodeSummary'), dict) else {}
        sent = strict_time(last.get('sentAt'))
        if last.get('digest') == pending['digest'] and sent is not None \
                and self.clock() - sent / 1000 < SUMMARY_EVERY_SECONDS:
            return None
        return pending

    def summary_preview(self) -> dict:
        """What the next summary would say, read-only: nothing is saved, sent or spent (`org summary`). It works
        without consent, so a person can read what they would share before they agree."""
        state = self.state()
        if state is None:
            raise OrgError('not-enrolled', 'this machine is not enrolled in organization %s' % self.org_id,
                           command=enroll_command(self.org_id, None))
        inputs = self.summary_inputs()                      # read without the lock: the scans can be large
        with self._locked():
            state = self.state() or state
            out = {'org': self.org_id, 'listed': self.accepts_summary(state), 'consent': summary_notice(state)['consent'],
                   'lastSent': {k: v for k, v in (state.get('nodeSummary') or {}).items() if k != 'digest'} or None}
            if not self.consents(state):
                out.update(said='harness summary: not shared (consent off)',
                           command='project-observatory full org summary --org %s --consent on' % self.org_id)
            try:
                pending = self.summary_for(state, (state.get('summarySequence') or 0) + 1, inputs)
            except OrgError as error:
                out.update(error=error.code, message=str(error)[:300])
                out.update({k: v for k, v in (('nextStep', error.next_step), ('command', error.command)) if v})
                return out
            out.update(summary=pending['summary'], omitted=pending['omitted'], excluded=pending['excluded'],
                       baseline='applied' if 'drift' in pending['summary'] else 'none')
            return out

    def register_shares(self) -> dict:
        """Derives this enrollment's registrations from the applied estate revision and delta entries and sends what
        changed, what is due again (weekly) and what waits on a backoff. Nothing at all before the server lists
        `share-registration/1`.

        `previouslyRegistered` is, as share-registration.md reads, the grant ids this node registered active toward
        this enrollment: every id whose active registration the server took (`active_registered`), every id whose
        active registration went out without an answer that refused it, and every id whose last registration is still
        held (`hold`), so a held withdrawal is derived, and sent, again."""
        with self._locked():
            state = self._require()
            if not self.accepts_shares(state):
                return {'sent': 0, 'held': 'protocol-not-listed'}
            record, entries = applied_records(self.node)
            if record is None:
                return {'sent': 0, 'held': 'no-applied-record'}
            record_digest = record.get('digest') if isinstance(record.get('digest'), str) else None
            now = int(self.clock())
            out = {'sent': 0, 'degraded': [], 'answers': {}}
            with self._db() as conn:
                previous = sorted(r['grant_id'] for r in conn.execute(
                    'SELECT grant_id, doc, gate, active_registered FROM registrations')
                    if r['active_registered'] or r['gate'] == 'hold'
                    or (json.loads(r['doc']).get('state') == 'active' and r['gate'] == 'send'))
                derived = share_registration.derive(record, entries, self.org_id, previous)
                out['degraded'] = derived['degraded']
                for registration in derived['registrations']:
                    grant_id, digest = registration['grantId'], share_registration.digest(registration)
                    row = conn.execute('SELECT * FROM registrations WHERE grant_id = ?', (grant_id,)).fetchone()
                    if row is not None and row['digest'] == digest:
                        if row['gate'] == 'hold-degraded' and row['record_digest'] == record_digest:
                            continue                       # a typed refusal waits for the record to change
                        if row['gate'] == 'send' and registration['state'] == 'withdrawn':
                            continue                       # a withdrawal is final once taken
                        if row['gate'] in ('send', 'hold-notice') and row['sent_at'] \
                                and now - row['sent_at'] < share_registration.RESEND_SECONDS:
                            continue                       # DEC-0100: a notice refusal waits for the weekly resend
                        if row['gate'] == 'hold' and row['next_at'] > now:
                            continue
                    status, answer = self._signed(state, self.paths['share-registration'], registration)
                    if isinstance(state.get('removed'), dict):  # DEC-0103 `stop`: the organization ended
                        out.update(held='removed', removed=state['removed'])
                        self.journal('share-registration', grantState=registration['state'], status=status,
                                     gate='stop', code=_error_code(answer))
                        return out
                    if status == 200 and isinstance(answer, dict):
                        shaped = {'status': 200, 'receipt': answer}
                    elif status == 401:
                        shaped = {'status': 401, 'code': _error_code(answer)}
                    elif isinstance(answer, dict) and isinstance(answer.get('error'), dict):
                        shaped = {'status': status, 'body': answer}
                    else:
                        shaped = {'status': status}
                    gate = share_registration.gate(shaped)
                    active = registration['state'] == 'active'
                    was = bool(row['active_registered']) if row is not None else False
                    if gate == 'send':
                        registered = active                # taken: active is registered, a withdrawal ends it
                    elif gate == 'hold':
                        registered = was or active         # unanswered: it may have landed
                    else:
                        registered = was                   # refused: nothing changed on the server
                    attempts = (row['attempts'] + 1) if row is not None and gate == 'hold' else 0
                    conn.execute(
                        'INSERT INTO registrations(grant_id, doc, digest, answer, gate, record_digest, sent_at, '
                        'attempts, next_at, active_registered) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
                        'ON CONFLICT(grant_id) DO UPDATE SET doc = excluded.doc, digest = excluded.digest, answer = '
                        'excluded.answer, gate = excluded.gate, record_digest = excluded.record_digest, sent_at = '
                        'excluded.sent_at, attempts = excluded.attempts, next_at = excluded.next_at, '
                        'active_registered = excluded.active_registered',
                        (grant_id, json.dumps(registration, sort_keys=True), digest, json.dumps(shaped, sort_keys=True),
                         gate, record_digest, now, attempts, self._backoff(attempts) if gate == 'hold' else 0,
                         int(registered)))
                    self.journal('share-registration', grantState=registration['state'], status=status, gate=gate,
                                 code=_error_code(answer) if status != 200 else (answer or {}).get('state'))
                    out['sent'] += 1
                    out['answers'][grant_id] = gate
            return out

    def _register_quietly(self) -> dict:
        """Registrations sent on a change (an enrollment, an applied revision, a protocol list), not only on the
        schedule; a failure is the schedule's to retry."""
        try:
            result = self.register_shares()
            return {k: result[k] for k in ('sent', 'held') if k in result}
        except (OrgError, OSError, sqlite3.Error, ValueError, RuntimeError) as error:
            code = getattr(error, 'code', type(error).__name__)
            self.journal('share-registration-deferred', error=code)
            return {'deferred': code}
    def usage_days(self, until_ms: int) -> List[dict]:
        """The meter's settled grant-check receipts summed per (UTC day, grant), for ended days whose every
        reservation is settled or past its expiry: a day is never summed while a call of it may still settle (a
        reservation lives `meter.RESERVATION_SECONDS`, and may settle after midnight). `waiting` counts the others."""
        if self.meter_db is None or not Path(self.meter_db).exists():
            return []
        import paths as _paths
        now = until_ms // 1000
        conn = sqlite3.connect(_paths.sqlite_uri(self.meter_db), uri=True, timeout=30)
        try:
            rows = conn.execute(
                "SELECT date(created_at, 'unixepoch') AS day, grant_id, COUNT(settled_usd) AS calls, "
                "SUM(settled_usd) AS cost, SUM(CASE WHEN settled_usd IS NULL AND expires_at > ? THEN 1 ELSE 0 END) "
                "AS open FROM reservations WHERE created_at < ? GROUP BY day, grant_id ORDER BY day, grant_id",
                (now, (until_ms // 86400000) * 86400)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [{'day': r[0], 'grantId': r[1], 'calls': int(r[2]), 'costUsd': round(float(r[3] or 0), 6),
                 'waiting': bool(r[4])} for r in rows if r[2] or r[4]]
    def collect_grant_usage(self) -> dict:
        """Enqueues one `grant.usage` per (ended UTC day, grant) for the share grants the applied revision places in
        this organization's context, from the collection start on, each only when the grant's registration answer
        lets it (`gate`) and once none of that day's reservations can still settle; a held grant's days wait in the
        meter and are enqueued once the gate opens."""
        with self._locked():
            state = self._require()
            start = strict_time(state.get('collectionStart'))
            if start is None:
                return {'enqueued': 0, 'held': 'no-collection-start'}
            record, _ = applied_records(self.node)
            context = 'org:' + self.org_id
            grants = {g['meta']['id'] for g in (record or {}).get('shareGrants', []) if isinstance(g, dict)
                      and g.get('context') == context and isinstance(g.get('meta'), dict)
                      and share_registration.is_share_grant_id(g['meta'].get('id'))}
            with self._db() as conn:
                gates = {r['grant_id']: r['gate'] for r in conn.execute('SELECT grant_id, gate FROM registrations')}
            enqueued, held, waiting = 0, set(), 0
            for day in self.usage_days(int(self.clock() * 1000)):
                if day['grantId'] not in grants or strict_time(day['day'] + 'T00:00:00Z') < start:
                    continue
                if day['waiting']:
                    waiting += 1
                    continue
                if gates.get(day['grantId']) != 'send':
                    held.add(day['grantId'])
                    continue
                data = {'day': day['day'], 'grantId': day['grantId'], 'context': context, 'calls': day['calls'],
                        'unpricedCalls': 0, 'costUsd': day['costUsd']}
                if self.enqueue('grant.usage', data, occurred=day['day'] + 'T00:00:00Z',
                                key='grant.usage|%s|%s' % (day['day'], day['grantId'])):
                    enqueued += 1
            if enqueued:
                self.journal('grant-usage-enqueued', events=enqueued)
            return {'enqueued': enqueued, 'heldGrants': len(held), 'waitingDays': waiting}
    # -- the schedule ------------------------------------------------------------------------------------------
    def tick(self) -> dict:
        """What is due, in order: rotation, check-in, share registrations, grant usage, the outbox. Each step's
        failure is recorded and the next still runs. The whole tick holds the enrollment's lock; what a harness summary
        is built from is read before it is taken (the scans can be large)."""
        current = self.state() or {}
        due = self.clock() >= (current.get('nextCheckInAt') or 0)
        inputs = self.summary_inputs() if due and self._summary_wanted(current) else None
        with self._locked():
            state = self.state()
            if state is None:
                return {'org': self.org_id, 'result': 'not-enrolled'}
            said = notice(self.org_id, state, self.clock())
            step = {k: said[k] for k in ('removed', 'nextStep', 'command') if k in said}
            if said['state'] == 'removed':                 # recorded once, when it happened; nothing is retried
                return {'org': self.org_id, 'result': 'removed', **step}
            report: Dict[str, Any] = {'org': self.org_id}
            if renewal(state, self.clock()) is not None:   # DEC-0100: renewed by the key on record, then what is due
                try:
                    self._renew(state)
                except OrgError as error:
                    state = self.state()
                    said = notice(self.org_id, state, self.clock())
                    step = {k: said[k] for k in ('removed', 'nextStep', 'command') if k in said}
                    if error.code in ('renewal-pending', 'renewal-failed'):
                        return {'org': self.org_id, 'result': 'renewal-pending', 'renewal': {
                            k: v for k, v in (renewal(state, self.clock()) or {}).items() if k != 'by'}, **step}
                    result = 'removed' if said['state'] == 'removed' else 'needs-enrollment'
                    return {'org': self.org_id, 'result': result, 'error': error.code, **step}
                state = self.state()
                report['renewed'] = {'by': 'key-on-record', 'epoch': state['epoch'],
                                     'serial': state['certificate']['serial'],
                                     'notAfter': state['certificate']['notAfter']}
            if self.expired(state) or state.get('reEnroll'):
                self.journal('needs-enrollment', expired=self.expired(state))
                return {'org': self.org_id, 'result': 'needs-enrollment', **step}
            steps = [('rotate', lambda: self.rotate() if self.rotation_due(self.state()) else 'not-due'),
                     ('checkIn', lambda: self.check_in(force_renewal=False, inputs=inputs)
                      if self.clock() >= (self.state().get('nextCheckInAt') or 0) else 'not-due'),
                     ('shares', self.register_shares), ('grantUsage', self.collect_grant_usage),
                     ('outbox', self.flush)]
            for name, step in steps:
                try:
                    result = step()
                    report[name] = {'result': 'done'} if isinstance(result, dict) and 'device' in result else result
                except OrgError as error:
                    report[name] = {'error': error.code, 'message': str(error)[:200]}
                except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
                    report[name] = {'error': type(error).__name__}
            return report
    # -- status and leaving ------------------------------------------------------------------------------------
    def describe(self) -> dict:
        state = self.state()
        if state is None:
            return {'org': self.org_id, 'enrolled': False}
        said = notice(self.org_id, state, self.clock())
        cert_state = said['state'] if said['state'] in ('expiring', 'expired') else \
            'expired' if self.expired(state) else 'valid'
        out = {'org': self.org_id, 'enrolled': True, 'server': state['server'], 'device': state['device'],
               'state': 'enrolled' if said['state'] in ('valid', 'expiring') else said['state'],
               'epoch': state['epoch'], 'certificate': {'serial': state['certificate']['serial'],
                                                        'notAfter': state['certificate']['notAfter'],
                                                        'expiresOn': said['expiresOn'], 'state': cert_state,
                                                        'daysLeft': said['daysLeft'],
                                                        'rotationDue': self.rotation_due(state)},
               'protocols': state.get('protocols', []), 'shareRegistration': self.accepts_shares(state),
               'collectionStart': state.get('collectionStart'), 'lastCheckIn': state.get('lastCheckIn'),
               'checkInSeconds': state.get('checkInSeconds'), 'needsEnrollment': 'renewUntil' not in said
               and (self.expired(state) or bool(state.get('reEnroll'))),
               'rotationPending': isinstance(state.get('pendingRotation'), dict),
               'key': self._key_declaration(), 'nodeSummary': said['summary']}
        out.update({k: said[k] for k in ('removed', 'nextStep', 'command', 'renewUntil', 'renewalRefused',
                                         'purposeHolds') if k in said})
        out['outbox'] = {'pending': 0, 'acked': 0, 'rejected': 0, 'dropped': 0, 'lastBatch': None, 'retry': None}
        out['registrations'] = {}
        path = self.folder / 'outbox.sqlite'
        # Read-only, and nothing is created: a status read while `leave` runs never brings the folder back.
        if path.is_file():
            import paths as _paths
            conn = sqlite3.connect(_paths.sqlite_uri(path), uri=True, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                counts = {r['state']: r['n'] for r in conn.execute(
                    'SELECT state, COUNT(*) AS n FROM events GROUP BY state')}
                out['outbox'].update({k: counts.get(k, 0) for k in ('pending', 'acked', 'rejected', 'dropped')})
                out['outbox'].update(lastBatch=self._meta(conn, 'lastBatch'), retry=self._meta(conn, 'retry'))
                out['registrations'] = {r['gate']: r['n'] for r in conn.execute(
                    'SELECT gate, COUNT(*) AS n FROM registrations GROUP BY gate')}
            except sqlite3.Error:
                out['outbox']['unreadable'] = True
            finally:
                conn.close()
        degraded = []
        if 'removed' in out:
            degraded.append('org:removed')
        elif out['needsEnrollment']:
            degraded.append('org:needs-enrollment')
        if cert_state == 'expired' and 'removed' not in out:
            degraded.append('org:certificate-expired')
        elif cert_state == 'expiring':
            degraded.append('org:certificate-expiring')
        if not out['shareRegistration']:
            degraded.append('org:share-registration-not-listed')
        if out['registrations'].get('hold-degraded'):
            degraded.append('org:share-registration-refused')
        summary_state = out['nodeSummary']['state']
        if summary_state in ('not-listed', 'unavailable', 'refused'):
            degraded.append('org:node-summary-' + summary_state)
        if out.get('purposeHolds'):
            degraded.append('org:purpose-held')
        if out['outbox']['pending'] and (out['outbox']['retry'] or {}).get('attempts'):
            degraded.append('org:outbox-retrying')
        out['degraded'] = degraded
        return out

    def _key_declaration(self) -> dict:
        """What the enrollment request declares about the key, read without loading or creating a key."""
        if self._keys is None:
            from .device_keys import DeviceKeys
            source = DeviceKeys
        else:
            source = self._keys
        return {'storage': getattr(source, 'storage', 'file'), 'exportable': bool(getattr(source, 'exportable', True))}

    def leave(self) -> dict:
        """Removes what enrollment created, locally: this organization's folder, its own device keys with it (they
        belong to this enrollment and no other). The server keeps its records until an administrator retires the
        device. Under the lock, so a tick in flight finishes first and the next one finds nothing to recreate."""
        with self._locked():
            if not self.folder.exists():
                return {'org': self.org_id, 'left': False, 'reason': 'not-enrolled'}
            if self.folder.is_symlink() or not self.folder.resolve().is_relative_to((self.node / 'orgs').resolve()):
                raise OrgError('refused', 'the organization folder is not inside this node')
            shutil.rmtree(self.folder)
            self._keys = None
            return {'org': self.org_id, 'left': True}


def enrolled_orgs(node: Path) -> List[str]:
    folder = Path(node) / 'orgs'
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_dir() and ORG_ID.fullmatch(p.name)
                  and (p / 'enrollment.json').exists())
