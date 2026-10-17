# The agent-network node

The node is the part of the engine that lets one person's machines work as one network: each machine proves its device
key to a relay, sends and receives signed messages and sealed blobs through it, and believes a peer only for a signature
under a key the person's estate record lists. It is the client half of the contract's `fabric-relay/0.1` (DEC-0047,
DEC-0066) and serves the organization roadmap's **RM-21** (the agent network), stage 1: two of one person's own machines.

The relay is a router, not a party to trust. A delivered body is believed only when:

- it carries the sender's ES256 signature under the `#sig` key of a verified estate record;
- its signed recipient is this machine's own kid, never a value the relay asserts;
- its signed `exp` has not passed, and it bounds the body's life and the replay window.

## Threat model (stage 1)

Stage 1 connects one person's own machines through the person's own relay: every admitted machine and the relay are
the operator's. In scope, and blocking for the node's code:
- a stranger's input (anyone who is not an admitted machine, and anything a relay forwards from one);
- forgery and impersonation of an admitted machine;
- an input that crashes or wedges the node, or ends its reconnect loop;
- the loss of an authentic message.

Out of scope until stage 3 (shared networks, a relay that is not the operator's own), recorded as rows:
- availability attacks that need a hostile relay: a frame, an upgrade or a blob trickled to stretch a deadline
  ([OBS-115](../backlog.md));
- an admitted machine filling another's mailbox with bodies dated ahead or from a sender not listed yet
  ([OBS-116](../backlog.md));
- the remaining hardening of the review rounds ([OBS-117](../backlog.md)).

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

**Where the node is stricter than the reference.** Each answer is still one of the reference's codes:
- a document nested deeper than 64 levels, or larger than a frame (1 MiB), is `malformed`, not parsed. A hostile relay's
  deeply nested frame would otherwise exhaust the parser and stop the session's reader;
- a device key's base64 must be its one canonical spelling, padded standard base64; a signature, an encapsulated key
  and a JWK coordinate are unpadded base64url with no `+`, `/` or `=`, and also only in their canonical spelling;
- a time offset is at most `±23:59` (`+24:00` and `+12:60` are no offset, as JavaScript reads them);
- a `#sig` kid counts only when it is its own machine's `<id>#sig`, and a kid listed twice counts for neither machine.
  A verified record already holds both by FAC-SEM-073; the node checks them again before it believes a key.

Times read as JavaScript's `Date` reads them, for every year 0000-9999 (`calendar.timegm` refuses year 0). Only ASCII
digits are accepted, and nothing may follow the closing `Z`.

Parsing and signing share one bound: `relay.parse_json` refuses more than 64 levels (`MAX_JSON_DEPTH`) and
`jcs.rfc8785` refuses the same, so a node never signs a document another node refuses to read.

**Device keys under concurrency.** `load_or_create` holds the folder's lock across the check, the generation and the
writes. Each file is created exclusively (written to a temporary file, then hard-linked), so two processes starting
together end with one key set, the one both return. A folder another account owns, or one others may write, is
refused, before the lock is taken (taking it would make the folder 0700 and hide the fault). A writer killed between
writing a key and linking it leaves a `.device-key-*` temporary file; the next start removes it under the lock, so the
recovery is to start again, and no stale private key stays on disk.

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
    take stays in the mailbox and is delivered again on the next connection;
  - a refused, repeated or unreadable body is acknowledged first, then reported to `on_refused`, without acting on it;
- `presence`, `heartbeat` and `delta` go to their callbacks unchanged;
- a 4403 close (this machine left the admission set, or the record expired) ends the session, and a call after it
  raises `SessionClosed`.

**Nothing a relay sends ends the reader except a close.**
- A check that raises refuses the body as `malformed`, and it is acknowledged.
- A callback that raises is logged.
- A frame nested too deep is skipped.
- Any other failure closes the session with a recorded reason (`relay.reader.<error>`), never half alive.

**A dead link is closed:** after two pings in a row go unanswered, or after 75 s of silence, which is shorter than the
relay's own 90-second idle rule. `WebSocket.abort` wakes the blocked reader with `shutdown`. It leaves the descriptor for
the reader to close: closing it from another thread races the pending `recv`, which then waits out its whole timeout.

`keep_connected` reopens a closed session with a jittered, doubling backoff. It stops for good on 4401 and 4403, and on
a refused `auth`, because a relay would refuse every retry until a person acts.

The WebSocket client refuses:
- a frame over 1 MiB, or a message over 1 MiB or 1,024 fragments;
- text that is not UTF-8;
- a control frame over 125 bytes;
- a reserved bit;
- an unknown opcode;
- a binary or masked server frame.

It also answers each close as RFC 6455 §7 says:
- a one-byte payload is 1002;
- a reason that is not UTF-8 is 1007;
- 1005, 1006 and 1015 are never echoed.

**What is acknowledged.** A body refused for a fault in it (`malformed`, `bad-signature`, `mismatch`, `bad-key`,
`unsupported-alg`, `lifetime-too-long`, `expired`, `replay`) is acknowledged. A body refused as `unknown-sender` or
`not-yet-valid` is reported and left unacknowledged: a newer estate revision may list the sender, and a clock may catch
up, so the relay delivers it again on the next connection until its `ttl`. When the node's own lookup or replay store
fails while it checks a body, the body may be authentic: it stays unacknowledged and the session closes, so the next
connection delivers it again.

`keep_connected` gives every session it opens one `ReplayStore`, so a message handled just before a link dropped, and
delivered again after it, is acted on once. The opening (`hello`, `auth`, `welcome`) has one 10-second deadline, so a
relay that keeps pinging cannot hold it open. A blob exchange has a 300-second total deadline, and an error body is read
up to 64 KiB.

The replay window is in memory. A node that restarts within a message's life may act on one redelivery again, and the
payload's own idempotency key absorbs that. Logs carry event names, kids, codes and counts, never a body.

`tests/test_node_session.py` runs the session against a relay stub on `127.0.0.1`, which relay.md allows for a relay on
the same machine. The stub admits by the signed `relay-auth/1`, answers with `replyTo`, keeps mail in `seq` order and
deletes only what is acknowledged. It can also misbehave the way a relay may:
- name another origin;
- deliver a forged, misdirected or repeated body, even asserting a `to` that matches it;
- close a revoked session with 4403.

49 planted defects were run against `tests/test_node_session.py` (raw loopback servers drive the RFC 6455 cases), and
48 fail it. The last is equivalent: a close without a code is answered without one either way, because 1005 is never
sendable. Among them:
- masking: a constant or non-CSPRNG mask, a missing mask bit;
- every size, fragment, UTF-8, control-frame, RSV and opcode check removed, and a new message inside a fragmented one;
- the `Sec-WebSocket-Accept`, subprotocol and TLS certificate checks removed;
- no pong; the close-code rules, the sendable-code filter included;
- no hello deadline, or one per read instead of one for the whole opening; no 75-second receive timeout;
- no origin check; acknowledging before the handler, or calling `on_refused` before the acknowledgement;
- no replay check, or a fresh replay store for each reconnect;
- trusting the relay's `to`;
- a local lookup failure taken for a malformed body; a transient refusal acknowledged;
- either catch-all removed, or the callbacks unguarded;
- the dead link never closed;
- reconnecting that never stops, stops on every close, retries a refused `auth` or a 4403 during opening, never
  doubles its backoff, has no jitter or ignores `retryAfter`;
- blob redirects followed through either opener; an unbounded blob read, error body or total time; no size or digest
  check; an error body of another shape crashing the client.

## What comes next

| Slice | What |
|---|---|
| 3 | estate-record verification, revocation deltas and the heartbeat, with the 72-hour freshness rule |
| 4 | sealed envelopes (HPKE, suite 16/1/1, info `fabric-secret-envelope/1`) to a peer's `#kem` |
| 5 | the enrollment client, once the contract's machine ceremonies are decided |
| 6 | the node service and its CLI |
