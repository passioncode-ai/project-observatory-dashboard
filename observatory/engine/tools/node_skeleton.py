#!/usr/bin/env python3
"""The agent network's walking skeleton on one machine (RM-21 stage 1): two machines' nodes exchange signed messages
and a blob through a real relay, end to end, on loopback.

    python tools/node_skeleton.py --relay-source PATH [--keep]

PATH is a checkout of the PassionCode relay with its dependencies installed (`npm ci`); its portable build
(`src/portable/main.ts`) is started on 127.0.0.1 with a temporary data folder. Everything else is this engine's node:
device keys, the estate record the relay admits from, `RelaySession`, `put_blob` and `get_blob`.

**The estate is a test estate, not the real ceremony.** Its revision 1 is signed by the contract vectors' published
EdDSA test passkey (anyone can derive it; nothing real trusts it), because the real ceremony (the operator's passkey
on the hub, DEC-0084) does not exist yet. The test estate lives only in the temporary folder and is never written to
the operator's workspace. Prints one JSON receipt; exit 0 when every step held, 1 when one did not, 2 when the relay
could not start.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from node import estate, p256, relay                    # noqa: E402
from node.blobs import get_blob, put_blob               # noqa: E402
from node.device_keys import DeviceKeys                 # noqa: E402
from node.session import RelaySession                   # noqa: E402

VECTORS = ENGINE / 'fabric' / 'node-vectors' / 'estate-record.json'
TEST_ESTATE = 'estate-skeleton-test'
WAIT = 15.0


def test_passkey():
    """The contract vectors' published EdDSA test passkey: its private key and the authority a relay pins."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    keys = json.loads(VECTORS.read_text(encoding='utf-8'))['keys']['passkeyEdDSA']
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(keys['seed'])), keys['authority']


def sign_test_revision(body: dict) -> dict:
    """Revision 1 signed as a passkey would sign it (WebAuthn assertion over `estate-revision/1`), by the test key."""
    key, authority = test_passkey()
    digest = estate.estate_record_digest(body)
    auth_data = hashlib.sha256(authority['rpId'].encode()).digest() + bytes([0x05]) + (1).to_bytes(4, 'big')
    client = json.dumps({'type': 'webauthn.get', 'challenge': p256.b64url_encode(estate.estate_challenge(digest)),
                         'origin': authority['origins'][0], 'crossOrigin': False}, separators=(',', ':')).encode()
    sig = key.sign(auth_data + hashlib.sha256(client).digest())
    return dict(body, digest=digest, signature={
        'format': 'webauthn-assertion', 'credentialId': authority['credentialId'], 'alg': 'EdDSA',
        'authenticatorData': p256.b64url_encode(auth_data), 'clientDataJSON': p256.b64url_encode(client),
        'sig': p256.b64url_encode(sig)})


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def start_relay(source: Path, data: Path, port: int):
    origin = 'http://127.0.0.1:%d' % port
    env = {**os.environ, 'RELAY_DATA_DIR': str(data), 'RELAY_HOST': '127.0.0.1', 'RELAY_PORT': str(port),
           'RELAY_ORIGIN': origin}
    log = open(data.parent / 'relay.log', 'w', encoding='utf-8')
    process = subprocess.Popen(['node', '--import', 'tsx', 'src/portable/main.ts'], cwd=source, env=env,
                               stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(origin + '/healthz', timeout=2) as answer:
                if answer.status == 200:
                    return process, origin
        except OSError:
            time.sleep(0.3)
    process.kill()
    raise RuntimeError('the relay did not start; see its log')


def run(source: Path, keep: bool) -> dict:
    work = Path(tempfile.mkdtemp(prefix='node-skeleton-')).resolve()  # paths-check: allow — removed in the finally below unless --keep
    receipt: dict = {'schema': 'node-skeleton/1', 'estate': TEST_ESTATE, 'testEstate': True, 'steps': {}}
    process = None
    try:
        machines = {name: DeviceKeys.load_or_create(work / name / 'device') for name in ('laptop', 'desk')}
        now = int(time.time()) * 1000
        _, authority = test_passkey()
        record = sign_test_revision({
            'schema': estate.ESTATE_RECORD_SCHEMA, 'estate': TEST_ESTATE, 'revision': 1,
            'issuedAt': relay.rfc3339(now - 60_000), 'validUntil': relay.rfc3339(now + 86_400_000),
            'authorities': [authority],
            'machines': [{'id': keys.device_id, 'deviceKeys': keys.enrollment_keys()} for keys in machines.values()],
            'revocations': []})
        verdict = estate.verify_estate_record(record, at_ms=now, authorities=[authority])
        receipt['steps']['record'] = verdict['ok']
        if not verdict['ok']:
            receipt['problem'] = verdict
            return receipt
        data = work / 'relay-data'
        (data / 'estate' / 'revisions').mkdir(parents=True)
        (data / 'estate' / 'pinned.json').write_text(json.dumps([authority]))
        (data / 'estate' / 'revisions' / '1.json').write_text(json.dumps(record))
        process, origin = start_relay(source, data, free_port())
        receipt['relay'] = origin
        peers = relay.peer_keys(record, int(time.time() * 1000))
        sender_key = peers['keys'].get
        inbox = {name: [] for name in machines}
        sessions = {}
        for name, keys in machines.items():
            session = RelaySession(origin, keys, TEST_ESTATE, sender_key,
                                   on_message=lambda m, n=name: inbox[n].append(m),
                                   revoked=set(peers['revoked']).__contains__)
            sessions[name] = session
            welcome = session.open()
            receipt['steps']['admitted:' + name] = welcome.estate_revision == 1
        laptop, desk = machines['laptop'], machines['desk']
        sessions['laptop'].send(desk.sig_kid, {'command': 'ping', 'from': 'laptop'}, msg_id='skeleton-1')
        sessions['desk'].send(laptop.sig_kid, {'command': 'pong', 'from': 'desk'}, msg_id='skeleton-2')
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and not (inbox['desk'] and inbox['laptop']):
            time.sleep(0.05)
        receipt['steps']['laptop->desk'] = [m['payload'] for m in inbox['desk']] == [{'command': 'ping', 'from': 'laptop'}]
        receipt['steps']['desk->laptop'] = [m['payload'] for m in inbox['laptop']] == [{'command': 'pong', 'from': 'desk'}]
        notes = receipt.setdefault('notes', {})
        try:
            presence = sessions['laptop'].presence([desk.sig_kid])
            receipt['steps']['presence'] = [p['state'] for p in presence['presences']] == ['online']
        except Exception as error:                     # each step is reported, none ends the run
            receipt['steps']['presence'] = False
            notes['presence'] = type(error).__name__ + ': ' + str(error)
        try:
            blob = os.urandom(100_000)
            digest, _ = put_blob(origin, laptop, blob, [desk.sig_kid])
            receipt['steps']['blob'] = get_blob(origin, desk, digest, [desk.sig_kid]) == blob
        except Exception as error:
            receipt['steps']['blob'] = False
            notes['blob'] = type(error).__name__ + ': ' + str(error)
        for session in sessions.values():
            session.close()
        return receipt
    finally:
        receipt['ok'] = bool(receipt['steps']) and all(receipt['steps'].values())
        if process is not None:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
        if keep:
            receipt['kept'] = str(work)
        else:
            shutil.rmtree(work, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='The agent network walking skeleton: two nodes through a local relay.')
    parser.add_argument('--relay-source', required=True, type=Path, help='a relay checkout with npm ci done')
    parser.add_argument('--keep', action='store_true', help='keep the temporary folder (test keys only) to read the logs')
    args = parser.parse_args(argv)
    source = args.relay_source.resolve()
    if not (source / 'src' / 'portable' / 'main.ts').is_file() or not (source / 'node_modules').is_dir():
        print(json.dumps({'ok': False, 'error': 'not a relay checkout with its dependencies installed'}))
        return 2
    try:
        receipt = run(source, args.keep)
    except RuntimeError as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 2
    print(json.dumps(receipt, indent=1))
    return 0 if receipt['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
