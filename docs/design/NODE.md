# The agent-network node

The node is the part of the engine that lets one person's machines work as one network: each machine proves its device
key to a relay, sends and receives signed messages and sealed blobs through it, and believes a peer only for a signature
under a key the person's estate record lists. It is the client half of the contract's `fabric-relay/0.1` (DEC-0047,
DEC-0066) and serves the organization roadmap's **RM-21** (the agent network), stage 1: two of one person's own machines.

The relay is a router, not a party to trust. A delivered body is believed only when:

- it carries the sender's ES256 signature under the `#sig` key of a verified estate record;
- its signed recipient is this machine's own kid, never a value the relay asserts;
- its signed `exp` has not passed, and it bounds the body's life and the replay window.

## What is built (OBS-107, slice 1: the foundation)

| Module | What it does | Reference it is held to |
|---|---|---|
| `observatory/engine/node/jcs.py` | RFC 8785, the bytes every relay signature covers | contract `src/canonical.ts`, `fixtures/vectors/canonical.json` |
| `observatory/engine/node/p256.py` | SEC 1 §3.2.2 key validation; ES256 sign and verify, low-S only | `src/p256.ts`, `fixtures/vectors/p256.json` |
| `observatory/engine/node/relay.py` | what a node signs (`auth`, message bodies, `Fabric-Relay-Request`); how it checks a delivered body, a replay and its peers' keys | `src/relay.ts`, `fixtures/vectors/relay.json` |
| `observatory/engine/node/device_keys.py` | the device's `#sig` (ES256) and `#kem` (ECDH P-256) keys in an owner-only store | contract DEC-0053, DEC-0056 |

The vectors are vendored byte for byte in
[`observatory/engine/fabric/node-vectors/`](../../observatory/engine/fabric/node-vectors/README.md), with the contract
commit and digests. `tests/test_node_foundation.py` runs every case through the ports, including:

- the relay's own `admit` and blob checks, run over the node's signing inputs;
- round trips: what this node signs, a relay and a peer accept.

Each check was also run against a planted defect, and the suite failed every time:

- key order by code point instead of UTF-16;
- a wrong exponent threshold;
- no low-S normalisation;
- high-S accepted;
- an open JWS header;
- an unchecked recipient;
- rounded fractions of a second;
- a revoked machine kept;
- a changed `relay-auth/1` input.

Signing needs the engine's `full` extra (`cryptography`). Canonical JSON and key validation use the standard library
alone.

**Device keys.** The store keeps the two keys as PKCS#8 files: 0600 in a 0700 folder, written atomically through
`private_io`, refusing symbolic links. It never replaces keys already present. A replaced key is a lost device, because
the estate record still names the old one; replacing a key is the record's own ceremony. A half-written set is refused,
not completed. The device id is opaque (`m-` and 32 hex digits) and names no person or machine. A hardware-held `#sig`
(Secure Enclave, TPM) takes the file's place behind the same `sign` once the native key helper exists.

## The relay session (OBS-108, slice 2)

| Module | What it does |
|---|---|
| `observatory/engine/node/ws.py` | a WebSocket client (RFC 6455) on the standard library: text frames, client masking, a ping answered, a close raised with its code, 1 MiB per message; TLS by the platform's default context; plain `ws://` only on a loopback literal |
| `observatory/engine/node/session.py` | `RelaySession`: one machine's connection to `wss://<relay>/v1/connect` with the subprotocol `fabric-relay.v1` |
| `observatory/engine/node/blobs.py` | `put_blob` and `get_blob`: `PUT`/`GET /v1/blobs/<digest>` with a signed `Fabric-Relay-Request`; no redirects followed; fetched bytes returned only when they hash to the digest asked for |

`RelaySession.open` reads `hello`, refuses a relay that names another origin than the one dialled before it signs
anything, answers `auth` and waits for `welcome`. A 4401 refusal carries no reason, by design: the error says what the
person can check (this machine in the estate's current revision, then a current client). After `welcome`, one reader
thread owns the socket:

- an answer wakes the call that sent its frame: `send` returns the mailbox `seq` or raises the relay's `error`, and
  `presence` and `ping` work the same way;
- a `deliver` is checked end to end:
  - first against this machine's own kid, never a `to` the relay asserts, then against the replay window;
  - it is handed to the handler and acknowledged only after the handler returns, so a message the handler could not
    take is delivered again;
  - a refused or repeated body is acknowledged without acting on it;
- `presence`, `heartbeat` and `delta` go to their callbacks unchanged;
- a 4403 close (this machine left the admission set, or the record expired) ends the session, and a call after it
  raises `SessionClosed`.

The replay window is in memory. A node that restarts within a message's life may act on one redelivery again, and the
payload's own idempotency key absorbs that. Logs carry event names, kids, codes and counts, never a body.

`tests/test_node_session.py` runs the session against a relay stub on `127.0.0.1`, which relay.md allows for a relay on
the same machine. The stub admits by the signed `relay-auth/1`, answers with `replyTo`, keeps mail in `seq` order and
deletes only what is acknowledged. It can also misbehave the way a relay may:
- name another origin;
- deliver a forged, misdirected or repeated body, even asserting a `to` that matches it;
- close a revoked session with 4403.

Each of these planted defects failed the suite:
- no origin check;
- acknowledging before the handler;
- no acknowledgement of a refused body;
- no replay check;
- trusting the relay's `to`;
- returning fetched blob bytes without checking their digest.

## What comes next

| Slice | What |
|---|---|
| 3 | estate-record verification, revocation deltas and the heartbeat, with the 72-hour freshness rule |
| 4 | sealed envelopes (HPKE, suite 16/1/1, info `fabric-secret-envelope/1`) to a peer's `#kem` |
| 5 | the enrollment client, once the contract's machine ceremonies are decided |
| 6 | the node service and its CLI |
