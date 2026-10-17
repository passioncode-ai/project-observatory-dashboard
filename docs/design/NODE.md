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

## What comes next

| Slice | What |
|---|---|
| 2 | the relay session client: WebSocket `wss://<relay>/v1/connect`, subprotocol `fabric-relay.v1`; `hello` → `auth`; presence, mail and blobs |
| 3 | estate-record verification, revocation deltas and the heartbeat, with the 72-hour freshness rule |
| 4 | sealed envelopes (HPKE, suite 16/1/1, info `fabric-secret-envelope/1`) to a peer's `#kem` |
| 5 | the enrollment client, once the contract's machine ceremonies are decided |
| 6 | the node service and its CLI |
