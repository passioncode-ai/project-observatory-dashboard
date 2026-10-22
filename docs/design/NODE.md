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

**Whose fault a refusal is.**
- A hello that is not this protocol's (another program squatting a loopback port, a malformed nonce, another origin)
  is retried with backoff, never terminal: only the relay's own refusal of `auth` says this machine is not admitted.
- An `auth` frame this node cannot sign (its own estate, kid or keys) is a `local-fault`: logged, terminal, because
  no retry changes it.
- A deliver frame whose `from`, `msgId` or `seq` has the wrong type is `malformed` before any lookup. Signed
  identifiers compare as JavaScript's `!==` does, a value of another type never being equal.
- A sender the applied record revokes is `revoked-sender`, final and acknowledged, unlike one not listed yet.
  `relay.peer_keys` names the revoked kids.

`keep_connected` closes each session on every exit and logs a listener that raises without letting it end the loop.

Planted defects that fail the suites for these rules (#295):
- local-fault not terminal, or not logged;
- a refused opening's `retryAfter` ignored;
- pings stopping after an error answer;
- the catch-all not logging;
- a session closed only on stop;
- an unguarded listener;
- no shape check on a deliver frame;
- revoked senders not told apart;
- a squatter's hello made terminal;
- identifiers compared without their type;
- an empty revoked list;
- `wrong-origin` not named;
- a hello of another protocol not retried.

A client data nested past 64 levels is `malformed` in a passkey assertion, by the same bound as a relay frame
(`relay.json_depth_ok`).

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

## The estate record and its freshness (OBS-109, slice 3)

| Module | What it does | Reference |
|---|---|---|
| `observatory/engine/node/identifiers.py` | identifier equality by normal form (RFC 3986 §6.2.2, RFC 8141 §3.1) | `src/identifiers.ts`, `identifiers.json` |
| `observatory/engine/node/ed25519.py` | Ed25519 verification that refuses small-order keys first | `src/ed25519.ts` |
| `observatory/engine/node/webauthn.py` | offline passkey assertions (WebAuthn Level 3 §7.2) and the register of passkey-signed statements: a dedicated `sign.<domain>` RP, origins pinned within it | `src/webauthn.ts`, `src/signing-challenges.ts` |
| `observatory/engine/node/estate.py` | `verify_estate_record` in the ER table's order: shape, digest, chain to the parent (a revision never drops a revocation), the signing passkey among the trusted ones, the assertion, the window, then the record's own rules FAC-SEM-072…076, 092, 110, 111 | `src/estate-record-rules.ts`, `estate-record.json` |
| `observatory/engine/node/revocation.py` | the signed revocation delta, the heartbeat and freshness: `current`, `behind`, `stale` (no heartbeat for 72 hours) or `broken` (two signed histories); only `current` accepts what other machines send | `src/revocation-delta.ts`, `revocation-delta.json` |

A node believes a peer's `#sig` key only from a revision `verify_estate_record` accepted (`relay.peer_keys` reads it),
minus what the delta revokes (`revocation.effective_revocations`). `tests/test_node_estate.py` also re-signs revisions
with the vectors' EdDSA test passkey to hold each rule: two signing keys on one machine, a key named for another
machine, an off-curve device key, a small-order hub key, an unpinned publisher key, a credential-shaped secret slot, a
grant listed twice, a share grant in the personal context, an organization instance without its grant, a pinned origin
outside the signing RP, a missing user-verification flag, a sign-in RP and a ceremony inside a frame.

Planted defects that failed the suites:
- estate:
  - a dropped revocation allowed;
  - a revoked passkey signing;
  - a 61-second skew;
  - no secret detector;
  - a sign-in RP allowed;
  - BS without BE;
  - `topOrigin` ignored;
- delta:
  - no `prev` check;
  - no revoked-signer check;
  - the 72-hour bound off by one;
  - a small-order signer;
  - reconciliation skipping a kind;
  - no fork detection;
  - a 61-second skew.

## The secret envelope (OBS-113, slice 4)

`observatory/engine/node/envelope.py` ports `src/secret-envelope.ts` (DEC-0047 item 7, DEC-0066). A secret is sealed by
HPKE base mode (RFC 9180; DHKEM(P-256, HKDF-SHA256), HKDF-SHA256, AES-128-GCM; info `fabric-secret-envelope/1`) to the
target's `#kem` key. The header is the AEAD's additional data, and the sealer signs `{header, ct}` with its `#sig` key.

`open_envelope` checks in the reference's order:
1. shape;
2. algorithm;
3. recipient;
4. keys (SEC 1);
5. signature (low-S);
6. that the estate record lets the sender hold the slot;
7. the placement run;
8. the anti-rollback mark: an equal sequence passes only for the same envelope;
9. the times;
10. that it opens.

On success it returns the mark the target writes with the secret in one transaction. `estate_envelope_context` derives
the sender's key, who may hold a slot and the current placement from the applied revisions. It reads only personal
secret placements: an envelope names no context.

Sealing with RFC 9180 §A.3.1's ephemeral key reproduces the contract vector's header and ciphertext byte for byte. Six
planted defects fail `tests/test_node_envelope.py`:
- no sequence-reuse check;
- a placement ahead of the applied revision;
- another info string;
- no signature check;
- reading an organization's placement;
- ignoring a mark ahead of the run.

## This machine's identity from a terminal (OBS-114, slice 6, first part)

`project-observatory full node --init` creates the device keys if this machine has none, in the workspace's
`node/device/` (or `OBSERVATORY_NODE`/device), and prints a `node-identity/1` document: the device id, when it was
created, and the `deviceKeys` an enrollment and the estate record list (public keys only). Running it again changes
nothing and prints the same. `--show` prints it without creating anything, and exits 1 when there are no keys yet.
A machine's identity is not moved by copying the folder: a new machine runs `--init` and is added to the estate by its
own ceremony (DEC-0084).

## The estate ceremonies (OBS-133, slice 5)

The contract decides how a machine joins and leaves an estate in DEC-0084 (fabric-agent-contract #113). The node ports
its checks; the ceremony's screens and the passkey prompt belong to whatever surface runs it.

**Adding a machine by pairing** (`node/pairing.py`). The new machine and one trusted machine meet once at a relay
rendezvous:
- each first sends a commitment to its role, its device keys and a fresh 32-byte nonce (the trusted side also commits
  to the digest of the estate's revision 1), and only then reveals them;
- `check_reveal` opens each reveal against its commitment. A side that changed its keys or nonce after seeing the
  other's is refused, so an attacker in the middle gets one guess at the code;
- `checked_comparison_code` derives the six digits both screens show, and only after both reveals opened. The person
  compares them and only then signs the revision that lists the new machine;
- `rendezvous_verdict` burns the rendezvous for the first malformed, repeated, out-of-order or mismatched message, an
  abort, a message after both reveals, or anything past 600 s.

Before the new machine pins the estate and applies that revision, `join_listed` checks three things:
- the chain starts at the revision 1 the trusted side revealed. A copied passkey and a copied trusted key cannot start
  another estate there;
- the record lists the new machine with exactly its revealed keys;
- the record still lists the trusted machine with its `#sig` key.

`estate_fingerprint` shows revision 1's digest as five groups of five digits, so a person can compare two screens.

**What a revision drops, it revokes** (FAC-SEM-114). `verify_estate_record` with a parent refuses a revision that
drops a machine, or a machine's key, without a matching revocation in the same revision. A replaced key is revoked by
its key reference `<kid>@sha256:<fingerprint>` (`device_key_ref`), never by its kid, because the new key keeps the kid.
A malformed `@` reference is refused (FAC-SEM-076).

`own_keys_decision` is what a node does with a verified revision for its own keys:
- `apply`, destroying the held keys the revision no longer lists (`retire`);
- `stop` when the machine is removed;
- `stop` when its listed keys are ones it does not hold. Another machine now speaks for this id.

**Joining by an enroll token** (`node/enroll_token.py`). An `enroll-token/1` is signed by the estate's passkey for one
estate and lives at most 900 s. `verify_enroll_token` checks it in the contract's order: the estate, the authority
(revoked, duplicate or unknown), the passkey assertion, then the window.
- `redeem_enroll_token` is the hub's check: the joining machine's keys, its `enroll-join/1` proof over the token
  (`join_proof`, signed by its `#sig` key), revocation, and one use. Recording the use in the ledger is the hub's job.
- `read_token` takes the token only from stdin or an owner-only regular file of at most 64 KiB, opened without following
  a symlink. A token pasted as an argument, a URL or an environment variable is refused, and no error repeats the
  token.

Three changes follow on the relay side:
- `peer_keys` subtracts a key revoked by its key reference as well as by its kid, and lists references it cannot read in
  `unread`;
- `session_stands` keeps a session only while its kid still names the same key bytes;
- the secret envelope's open context takes the revocation-delta entries like the record's own (`extra_revocations`),
  which closes OBS-118.

An enroll token is a bearer until its first use: whoever holds it first joins as the machine it names. The surface that
runs the ceremony therefore shows the key fingerprint the token binds before the passkey signs it, and treats a
`reused` redemption as an alarm, never as a retry.

**Before installing a package** (`installation_findings`, the contract's `estate-placement` kind), a machine checks
that it is in the record, that the package is the one its digest names, that the record places that package on this
machine in this context (FAC-SEM-074), that every secret of the agent fills a slot the package declares (FAC-SEM-075),
and that the placement's share grant holds (FAC-SEM-111). The install itself is packet P6. A revocation delta entry
whose device-key ref holds `@` but is no key reference is refused as malformed, since no revision could list it.

`tests/test_node_pairing.py` and `tests/test_node_enroll.py` run every case of the contract's `pairing.json`,
`enroll-token.json` and the 70 `estate-*` fixtures of `semantic/`. Then they run the add, re-key and remove ceremonies
end to end between two device-key sets, on a test estate. The vectors are pinned to #113's merge commit `1531f21`.

## The walking skeleton

`python observatory/engine/tools/node_skeleton.py --relay-source PATH` runs the whole path on one machine:
- it starts the relay's portable build from a relay checkout (with `npm ci` done) on `127.0.0.1`, with a temporary data
  folder;
- it gives the relay a test estate whose revision 1 lists two machines' device keys;
- it opens one `RelaySession` per machine, sends a signed message each way, asks for presence, and puts and fetches a
  blob.

It prints one `node-skeleton/1` receipt with one step per stage. **The estate is a test estate, not the real
ceremony.** Its revision is signed by the contract vectors' published EdDSA test passkey, which nothing real trusts.
It lives only in the temporary folder.

First run, 2026-10-17, against relay `897be7d`:

| Step | Result |
|---|---|
| record verified | passed |
| both machines admitted | passed |
| a message laptop→desk | passed |
| a message desk→laptop | passed |
| blob put and get | passed |
| presence | failed |

Presence fails because that relay pins the contract at `51719e4`, before DEC-0066. It answers `presence-query` with
bare `presence` frames instead of one `presence-result` carrying `replyTo`, which the node (contract `bf3fd6b`)
waits for. The fix is the relay's contract bump.

Second run, 2026-10-18, against relay `00937e0` (relay#11, the contract bump to `bf3fd6b`): every step passed, presence
included (`"ok": true`).

## Organization enrollment (OBS-161)

**Contract pin: CT-65 (DEC-0094), merged at `de9eae4d`.** The contract admits the node's owner-only key file as
`storage: file` with `exportable: true` (and no attestation); every other store stays `exportable: false`. The schema
and the CT-65 device fixtures (`fabric/node-vectors/device/`: the file-key request, its three negatives, the check-in
answer with `protocols`) are vendored at that commit and run by `test_node_org.py`. A server still on an older
contract answers a 422 schema refusal pointing at `/key/storage` or `/key/exportable`, which the node reports as
`key-storage-refused`. `python3 tools/vendor_org_vectors.py --contract <checkout> --commit <sha>` re-pins every
OBS-161 file at another commit in one step. The rotation-replay fixtures are the receiver's rule (FAC-SEM-125); the
node's half, the same request sent again after a lost answer, is `test_a_rotation_lost_on_the_way_back_is_sent_again_unchanged`.

`node/org.py`, `node/http_signature.py`, `node/share_registration.py`, `tools/org.py` (`full org`). One enrollment per
organization (DEC-0082) in `node/orgs/<org.id>/`, with device keys of its own in `node/orgs/<org.id>/keys/` (devices.md:
each organization enrollment of a machine gets keys of its own), so no two organizations see one device id or key:

- **One writer at a time**: every command that changes an enrollment (enroll, rotate, check-in, enqueue, flush,
  registrations, grant usage, tick, leave), from the CLI or the maintenance job, holds the exclusive lock
  `node/orgs/.locks/<org.id>.lock` and reads the state again under it. Without it a check-in in flight during a
  rotation saved its older copy over the new certificate and the next check-in was refused `certificate-revoked`
  (adversarial round 1, reproduced); two check-ins spent one sequence number; a tick during `leave` recreated the folder.
  The lock file is outside the enrollment folder, so `leave` removing the folder never frees a lock another waits on.
- **Enrollment** (DEC-0031): `enrollment-request` with the opaque device id, `deviceKeys` (`#sig`, `#kem`), a PKCS#10
  request for the `#sig` key signed by it, `publicKeySha256` of its SubjectPublicKeyInfo, the key's `storage` as it
  is (`file`, `exportable: true` for the owner-only PKCS#8 file; CT-65, DEC-0094; a hardware store such as the Keychain
  helper of DEC-0053 plugs in through `Enrollment(keys=...)` with its own `storage`), and `auth.method`
  `enrollment-token` with the token id; the token itself goes in `Authorization: EnrollmentToken <id>.<secret>` and is
  read from a vault slot. The answer's certificate must bind this organization and device, certify this machine's
  `#sig` key, live at most 30 days, and come with an epoch above the one held. A server whose policy refuses a
  file-held key answers with a refusal the node names `key-storage-refused`. The receiver's paths are its own; the
  node uses the first organization server's (`/v1/devices/enroll`, `/check-in`, `/rotate`, `/v1/activity/batches`,
  `/v1/devices/share-registrations`), overridable per enrollment.
- **Transport** (DEC-0056): every later request carries `Content-Digest` and the `fabric-device` signature over
  `@method`, `@target-uri`, `content-digest` with `created`, a fresh 22-character `nonce`, `keyid` the certificate's
  serial and `alg` `ecdsa-p256-sha256`; a retry is signed afresh. `created` is on the server's clock as the last
  check-in measured it (`skewSeconds`); a `signature-stale` refusal forgets that measure and asks for a check-in at
  once. `verify_request` is the receiver's check in the
  contract's order, used by the tests' fake server; it answers all 54 cases of `http-signature.json` as the contract
  does.
- **Check-in and rotation**: a signed `check-in` with the sequence number, the collector stream (`observatory-node`,
  the server's epoch, `lastSeq`, `bufferedFrom`, unacknowledged dropped ranges) and the certificate; the answer sets the
  next interval, `collectionStart`, `report_full_state`, `certificate.action` and, when present, `protocols`, which
  replaces the held list as an enrollment or rotation answer does (DEC-0094). The check-in's `health` is `degraded`
  while the outbox retries or dropped events are undelivered. Rotation sends a new request from the same key under the
  current serial at two thirds of the lifetime or when asked; the request is persisted before it is sent and sent
  again unchanged after a lost answer, which the server answers with the same certificate (CT-65), and the previous
  certificate is kept until a 2xx under the new serial proves the server took it. A `certificate-unknown` before that
  goes back to the previous certificate and sends the request once more under it (a check-in rebuilt to name it, with
  a fresh sequence number); only a refusal of that one ends the enrollment. A `re-enroll` action, or an expired
  certificate past its renewal window (below), stops the schedule until the machine is enrolled again. `org status`
  reads the outbox read-only and creates nothing, so a status during `leave` never brings the folder back.
- **The certificate's expiry, said before it passes** (OBS-199): `org status`, `full doctor`
  (`organizations`) and the Health page read one `notice` per enrollment from its saved state, without the network,
  on the server's clock as the last check-in measured it (`skewSeconds`, the clock requests are signed on):
  the expiry date (`expiresOn`, `notAfter`) and a state, `valid`, `expiring` in the last three days
  (`EXPIRY_WARNING_SECONDS`, `org:certificate-expiring`, the step `org rotate --org ID` while the server answers),
  `expired` (`org:certificate-expired`), `needs-enrollment` or `removed`. A check-in, rotation, flush or registration
  on an expired certificate is refused before the network as `certificate-expired`, naming the date, the step in
  words (`nextStep`: a new enrollment token from the organization's administrator, put in a vault slot) and the
  `command` (`full org enroll --server ... --org ... --token-slot PROJECT/ENV/NAME`); `full org` exits 1 when every
  organization asked failed, a check-in the server refused included.
- **Renewal after expiry by the key on record** (OBS-199, contract DEC-0100): a certificate that ended at most 30 days
  ago (`RENEWAL_GRACE_SECONDS`, the contract's `RENEWAL_GRACE_DAYS`, the end included), judged on the server's clock
  (`skewSeconds`; a check-in the server refused as `certificate-expired` counts as ended), is renewed by `org check-in`
  and `org tick` before anything else, instead of being refused: an enrollment request to the enroll path with
  `auth: {method: "key-on-record"}` and nothing else in `auth`, no token and no `Authorization` header, a CSR from the
  enrollment's own `#sig` key (its signature is the proof) and `requestedAt` on the server's clock. Its answer is
  taken as any re-enrollment (the certificate checked, an epoch above every one before, registrations sent again),
  keeping `enrolledAt` and the measured skew and adding `renewedAt`; a certificate that binds another user or role
  than the one on record is refused (`binding-changed`, FAC-SEM-044). Inside the window `notice` keeps the state
  `expired` and adds `renewUntil`, the step in words and `full org check-in --org ID`; `needsEnrollment` stays false.
  An unanswered renewal (no answer, 408, 425, 429, 5xx) backs off (`renewal`: attempts, next time, last answer; the
  tick answers `renewal-pending`, a person's `org check-in` asks now); `certificate-revoked` or `certificate-unknown`
  with 401 removes the machine (OBS-204); any other refusal (a server pinned before DEC-0100 refuses `key-on-record`
  at its schema, and a server may refuse it by policy) is recorded for that certificate (`renewalRefused`) and never
  asked again, and from there, as past the 30 days, `certificate-expired` with the new-token step. A removed
  enrollment, or one the server asked to `re-enroll` or whose epoch it superseded, never renews. Every step runs under
  the enrollment's lock. The contract gives a node no way to know whether its server takes the renewal
  (`contractVersion` stays 0.1.0), so the node asks once and keeps the answer.
- **A machine removed from its organization** (OBS-204): a refusal `certificate-revoked` or `certificate-unknown` with
  HTTP 401, as the contract's step table sends them (`certificate-unknown` only after the fallback to a previous
  certificate above is refused too), to any signed request, ends the enrollment in `_signed`, for every caller; the
  same codes with any other status, and every 5xx, are retried as usual. `unknown-org` (a deleted organization) is
  provisional until the contract defines it: a 4xx with it is recorded (`unknownOrg`, shown as `org:unknown-org` with
  the date in status, doctor and Health) and retried, and ends the enrollment only when answered again at least an
  hour later and in a later tick (`tickSeq`, which `org tick` counts, so a wall clock that jumps within one tick does
  not count) with no successful request between (a success clears it). A batch refused with any of these codes, at
  any status, is held and retried with backoff, never rejected as content. An ended enrollment's pending events are
  dropped (state `dropped`, reason `removed:<code>`) with one
  count kept in the state's `removed` (`at`, `code`, `status`, `droppedEvents`), the retry and in-flight batch are
  forgotten, and from then on check-in, rotation, share registrations, grant usage, enqueue and flush are refused as
  `removed-from-organization` before the network, and `org tick` answers `removed` without retrying. `org status`,
  doctor and Health say so with the date and the code (`org:removed`). The contract gives no code that tells a closed
  organization from one revoked machine, so neither does the node. `org leave` removes the folder; `org enroll` with a
  new token starts over: a new stream (the old events, registrations and outbox state are cleared), an epoch the new
  server issues even if it is not above the old one, and the old removal kept as `previousRemoval`.
- **Outbox**: events persisted once with a ULID and the next seq before they are sent; batches are the oldest stream's
  consecutive run of at most 1000, keep their `batchId` across retries of the same slice, back off from 30 s to an
  hour, and are consumed by the ack (`seq`, `held`, `rejected`). A 200 that acknowledges none of the batch is held and
  retried under the same `batchId`. A batch refused for what it is (400, 422, `device-mismatch`) is consumed as
  rejected and never retried; a 413 halves the batch size first; 409 `activation-closed`, 429, 5xx and no answer are
  retried. Past 10 000 pending events the oldest are dropped down to 9 000 and named in one `buffer.overflow`: a later
  drop joins the newest overflow not yet sent, so a burst ends in one event.
- **Share registrations** (DEC-0086): derived by `share_registration.derive` (the contract's
  `deriveShareRegistrations`, all 51 derive cases) from `records/estate-record.json` and `records/delta-state.json`,
  which only `org records apply` writes after `verify_estate_record` and `apply_delta`. Sent only when the enrollment
  response lists `share-registration/1`; on change, after an enrollment, at least weekly; a typed refusal waits for the
  record to change; a 403 `purpose-not-collected` (DEC-0100, SR-5a: the person's acknowledged notice does not name a
  share registration's purposes) is `hold-notice`, held but not degraded, the sums kept, and sent again at the weekly
  resend; anything else (a held withdrawal included) retries with backoff. The answers are kept per grant.
  `previouslyRegistered` is what share-registration.md says: the grants this node registered active toward this
  enrollment, so a grant the server took, then a resend left held, is still withdrawn when it leaves the record.
  Registrations go out on a change (an enrollment, `org records apply`, a newly listed protocol), not only on the tick.
- **`grant.usage`**: one event per ended UTC day and share grant the applied revision places in `org:<org.id>`, from
  the meter's settled receipts (`calls`, `unpricedCalls` 0, `costUsd` their sum), from the collection start on, and
  only while `share_registration.gate` on the grant's latest answer says `send`. A day is summed only once none of its
  reservations can still settle (each is settled or past its expiry, `meter.RESERVATION_SECONDS` after it was made),
  so a call settled after midnight is counted. The gate is checked again at send: a `grant.usage` whose gate closed
  is dropped (named in a `buffer.overflow`) and its day enqueued again once the gate opens.

**Proven on every system** (OBS-201): `tests/test_node_org.py` runs in `full check` (it shipped unlisted in the
runner and ran nowhere until then; `tests/test_runner_evidence.py` now refuses an engine test file no runner names) on
the Linux and macOS rows, and as the enforced `org` step of the Windows job, after an import of `cryptography` so it
cannot pass by skipping. `test_the_device_keys_and_the_enrollment_are_owner_only_on_this_system` checks the key files,
`enrollment.json`, the outbox, the journal and their folders with the engine's own boundary (`osprivacy`): mode 0600
and 0700 on POSIX, an access list that grants only this account, LocalSystem and Administrators on Windows.

Not built here: verifying and applying the server's signed policy and key sets (OE-06; the check-in reports server
revision 0), a check-in scheduler at the server's interval (the maintenance job runs hourly), and the per-context
`kid` and `iss` (CX-CON-06 in the contract). A settlement the meter accepts after its reservation expired lands after
its day was sent and is not counted (the meter does not refuse late settlements).

## What comes next

| Slice | What |
|---|---|
| 5 | the ceremony surface: the pairing screens and the passkey prompt, in the app that runs them (the checks above are done) |
| 6 | the node service under the OS supervisor (`keep_connected` with the applied estate record), after slice 5 |
