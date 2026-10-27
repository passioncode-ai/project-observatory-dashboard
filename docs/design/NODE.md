# The agent-network node

The node is the part of the engine that lets one person's machines work as one network: each machine proves its device
key to a relay, sends and receives signed messages and sealed blobs through it, and believes a peer only for a signature
under a key the person's estate record lists. It is the client half of the contract's `fabric-relay/0.1` (DEC-0047,
DEC-0066) and serves the organization roadmap's **RM-21** (the agent network), stage 1: two of one person's own machines.

Where the node runs (the engine's local server), what supervises it, and which owner holds each verb on a machine
(install, check, roll back, supervise, show, compile skills, enroll, report) is decided in
[MACHINE-UTILITY.md](MACHINE-UTILITY.md), the machine-utility decision record (OBS-OE-00).

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
| `observatory/engine/node/estate.py` | `verify_estate_record` in the ER table's order: shape, digest, chain to the parent (a revision never drops a revocation), the signing passkey among the trusted ones, the assertion, the window, then the record's own rules FAC-SEM-072…076, 092, 110, 111, 122 (DEC-0090: a revision that breaks the partner channel's scope is refused whole, `rule-violation`) | `src/estate-record-rules.ts`, `estate-record.json` |
| `observatory/engine/node/revocation.py` | the signed revocation delta, the heartbeat and freshness: `current`, `behind`, `stale` (no heartbeat for 72 hours) or `broken` (two signed histories); only `current` accepts what other machines send | `src/revocation-delta.ts`, `revocation-delta.json` |

A node believes a peer's `#sig` key only from a revision `verify_estate_record` accepted (`relay.peer_keys` reads it),
minus what the delta revokes (`revocation.effective_revocations`). `tests/test_node_estate.py` also re-signs revisions
with the vectors' EdDSA test passkey to hold each rule: two signing keys on one machine, a key named for another
machine, an off-curve device key, a small-order hub key, an unpinned publisher key, a credential-shaped secret slot, a
grant listed twice, a share grant in the personal context, an organization instance without its grant, a partner's
publisher key scoped away from a personal placement and a scope naming a key the record does not pin (FAC-SEM-122), a
pinned origin outside the signing RP, a missing user-verification flag, a sign-in RP and a ceremony inside a frame.

**A partner's publisher key (OBS-171, contract DEC-0090, PA-06).** A partner (an integrator, an agency, an agent
studio) signs the packages it builds for a company with its own publisher key. The estate record pins that key in
`publisherKeys` like any other and names it in `publisherScopes[]`, `{kid, partner, contexts}`: the partner
organization that holds it (`org:<partnerOrgId>`) and the 1 to 64 organization contexts a placement under it may be
in, never `personal`. `placement_trust(record, machine, agentRef, context)` hands such a key only for a placement in
one of those contexts and answers no key (`{'publisher': []}`) elsewhere, so the package's signature finds no key and
the install is refused (`unknown-key`, PK-6). The key is always the record's own `publisherKeys` entry: the node reads
publisher keys from no other source, so a scope can never be sidestepped by a key that arrived another way. FAC-SEM-122
refuses, on a record, a scoped key outside its contexts, a key scoped twice and a scope naming a key the record does
not pin; `verify_estate_record` then answers `rule-violation` (ER-11) and the node keeps the revision it had. A key no
scope names keeps its earlier meaning, so a record without `publisherScopes` reads as before. The checks run over the
contract's five `estate-partner-*` fixtures and the cases of its `test/contexts.test.ts` (`tests/test_node_pairing.py`
`test_a_partners_publisher_key_is_trusted_only_in_its_scope`, `test_a_scope_is_read_as_the_reference_reads_it`), and
over re-signed revisions (`tests/test_node_estate.py` `test_each_rule`).

The rest of DEC-0090 is the hub's, the meter's and the adapter's. The hub signs a revision that carries
`publisherScopes` only once every machine of the estate is at or after DEC-0090, and mints under a partner grant only
once the meter is. This engine's delegation verifier (`delegation.py`, the meter's too) does not port the partner
actor yet: it refuses every token whose chain holds one at DL-13 row 5 (`malformed-chain`), the option DEC-0090 gives
a verifier (`tests/test_delegation_sealed.py` `test_a_partner_grant_token_is_refused_at_row_5`); and the meter's
`governance.schema.json` is still the one vendored at `7931c1d`, so a projected partner grant reads `unknown-state`, a
denial. Both stay open in OBS-171, the schemas' re-vendor with OBS-205.

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
its checks; the pairing screens and the passkey prompt are the ceremony surface below (OBS-197). The enroll-token
surface (the bound key fingerprint, `reused` as an alarm) and the re-key and removal ceremonies have no screen yet.

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
that the placement's share grant holds (FAC-SEM-111), and that a partner's publisher key is trusted in this context
(FAC-SEM-122, DEC-0090). The install itself is packet P6. A revocation delta entry
whose device-key ref holds `@` but is no key reference is refused as malformed, since no revision could list it.

`tests/test_node_pairing.py` and `tests/test_node_enroll.py` run every case of the contract's `pairing.json`,
`enroll-token.json` and the `estate-*` fixtures of `semantic/` (83 of the contract's 84). Then they run the add, re-key
and remove ceremonies end to end between two device-key sets, on a test estate. Since OBS-171 the whole `node-vectors/`
folder is at one commit, the engine's contract pin (`de8cec30`, the release `v0.1.0+2026-10-24.2`, since OBS-226;
`588748ab` from OBS-217; `1f31e5a` before it, after DEC-0090's
`3d8cb17`). The re-pin brought two estate rules the port now runs: FAC-SEM-134 (DEC-0106: a machine's `profile` is
`full`, `member` or `view`; only a full machine carries a hub key; a view machine has no placement, no secret and is
the source of none; `estate.machine_profile`) and FAC-SEM-137 (DEC-0107: a revision keeps its parent's `deltaAnchor`,
never at an earlier sequence or another hash at the same one; `estate.anchor_findings`, run by
`verify_estate_record` with a parent too). Until OBS-171 the estate set
was at #113's merge commit `1531f21`, and between the two only the `estate-*` fixtures changed: the five partner
fixtures, and share grant ids rewritten to `urn:fabric:share:` form, with the record digests that follow, in seventeen others.

## The ceremony surface (OBS-197, slice 5)

`node/ceremony.py` reads one pairing session through the checks above and answers the one screen a desktop app draws
(`node-ceremony/1`), so the Mac app and the Windows and Linux app show the same state for the same session and neither
decides a ceremony rule:

| State | When | What the person can do |
|---|---|---|
| `not-paired` | no session | Start pairing (as the new machine) |
| `hub-unreachable` | a step needs the hub and the hub is `not-ready` (it runs no pairing yet) or `unreachable` | Cancel pairing; nothing was sent |
| `waiting-for-hub` | the rendezvous is open (`step: other-side`), or the codes matched and the new machine waits for its revision (`step: revision`) | Cancel pairing |
| `code-shown` | `rendezvous_verdict` is `complete` and `checked_comparison_code` gave the six digits | The codes match, They differ (this side aborts) |
| `passkey` | the trusted machine's person confirmed the codes | Sign with passkey, enabled only when the hub is `ready`; the new machine and each key fingerprint are shown first |
| `confirmed` | the revision that arrived inside the rendezvous passes `join_listed` | nothing; it stands after the 600 s |
| `refused` | the rendezvous burned, the code could not be derived, or `join_listed` refused; `reason` is the check's own, `group` the sentence (`codes-differ`, `aborted`, `mismatch`, `other-estate`, `not-listed`, `trusted-not-listed`) | Start again |
| `expired` | the rendezvous passed 600 s | Start again |

The session lives in the workspace's `node/ceremony.json` (owner-only, beside `node/device/`). A desktop app reaches it
through the app protocol's `ceremony` action (`project-observatory full assistant ceremony`, `{"op": status | start |
confirm | differ | sign | cancel, "role"?}` on stdin, `{"screen", "hub"}` on stdout; refusals `not-now`,
`ceremony-in-progress`, `no-estate`, `hub-not-ready`, `device-keys-refused`, `invalid-input`). `start` as the new
machine creates the device keys when there are none, as `full node --init` does, and commits to them with a fresh
nonce; `start` as the trusted machine is refused with `no-estate`, because the engine keeps no applied estate record
until the node service (slice 6) applies one.

**What waits for the hub.** The rendezvous and the passkey page are the hub's side of DEC-0084 (Fabric CO-319 to
CO-321). Until a hub runs them, `hub_state()` answers `not-ready`: a started session shows `hub-unreachable`, nothing
is sent anywhere, `sign` is refused (`not-now`, since the passkey screen needs a hub; `hub-not-ready` once it is reached), and the screens past the code are reached only by the tests.
`tests/test_node_ceremony.py` drives every screen with the contract's `pairing.json`: each rendezvous case that carries
this machine's commitment (both roles) gives the screen its verdict implies, each join case gives `confirmed` or its
refusal, and the code shown is the vectors' checked code; then the steps on a temporary workspace and the app protocol.
The Mac app's `PairingView` (`macos/Sources/ObservatoryApp/Pairing.swift`, tests `CeremonyTests`) and the Windows and
Linux app's `ceremony` window (`desktop/ui/ceremony.js`, `main.rs` `ceremony_input`) draw it; the person's path is the
portable scenario OSS-94. Live acceptance follows Fabric CO-321.

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
OBS-161 file at another commit in one step, with the interop schemas kept at the pin; it reads and checks
the pin and every file before it writes any (OBS-205). The rotation-replay fixtures are the receiver's rule (FAC-SEM-125); the
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
  keeping `enrolledAt` and adding `renewedAt`, both on the server's clock: DEC-0102 judges a renewal by the server's
  clock, which the new certificate's `notBefore` reads, so `renewedAt` is that `notBefore` and the skew is measured
  again from it (kept as before when it is a day or more off); a purpose hold and the notice versions it waits on
  are kept too (the same person under the same notice); a certificate that binds another user or role
  than the one on record is refused (`binding-changed`, FAC-SEM-044). The person is compared by the binding's user
  id, which the certificate on record keeps (`certificate.userId`): a renewal without that id, or with another, is
  `binding-changed`, and a certificate whose `binding.user` carries no opaque string id (the contract requires one) is
  refused as `response-malformed`, nothing saved. Only a certificate saved by 0.29.0, which holds no id, has its first
  renewal compared by a person or none, and that renewal records the id (OBS-221). Inside the window `notice` keeps the state
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
  same codes with any other status, and every 5xx, are retried as usual. `unknown-org` is the first organization
  server's own enrollment refusal, not a contract code (DEC-0103): `org enroll` refused with it says the server does
  not know the organization id; on a signed request it is any other failure, retried, and the provisional rule that
  ended an enrollment on a second sighting an hour later (OBS-204's `unknownOrg` and `tickSeq`) is retired with
  OBS-217 (a state that still carries `unknownOrg` loses it at the next check-in taken). A batch refused with any of
  these codes, at any status, is held and retried with backoff, never rejected as content. An ended enrollment's pending events are
  dropped (state `dropped`, reason `removed:<code>`) with one
  count kept in the state's `removed` (`at`, `code`, `status`, `droppedEvents`), the retry and in-flight batch are
  forgotten, and from then on check-in, rotation, share registrations, grant usage, enqueue and flush are refused as
  `removed-from-organization` before the network, and `org tick` answers `removed` without retrying. `org status`,
  doctor and Health say so with the date and the code (`org:removed`). `org leave` removes the folder; `org enroll` with a
  new token starts over: a new stream (the old events, registrations and outbox state are cleared), an epoch the new
  server issues even if it is not above the old one, and the old removal kept as `previousRemoval`.
- **An organization that ended** (contract DEC-0103, OBS-217): a server answers any device request of an organization
  whose closing can no longer be cancelled with HTTP 410 and the body `device-organization-closed`
  (`{error: {code: "organization-closed", message}, closedAt}`), to a node pinned at or after DEC-0103. The node takes
  it as final on one answer, in `_signed` for every caller (check-in, batch, share registration, rotation): it ends
  that enrollment as above and drops whole what it buffered for that organization (the stream's rows, the share
  registrations and their answers; the meter's sums belong to no organization and stay), and the organization's
  `collectionStart`, policy root key, key set and any server policy layer it held. An `mdm` layer is the operating
  system's and stays; the machine's other enrollments are untouched. A batch answered 410 is the enrollment's end, not
  a rejection of its events; a share registration's gate answers `stop` for both the reference's shape and the wire's
  nested body (`share_registration.gate`). The state's `removed` adds `closedAt` and `droppedRegistrations`, and
  status, doctor and Health say the organization closed on that date (`org:removed`). Only the contract's exact body
  ends an enrollment (`organization_closed_at`, held to `device-organization-closed.schema.json` and its positive and
  negative fixtures): a 410 without a readable `closedAt`, with another code, a flat body or a path in its message is
  retried as any failure, because the end cannot be undone. The receiver's half, `http_signature.verify_request`,
  answers `{code: organization-closed, status: 410, closedAt}` at step 4, after the certificate is found and before its
  revocation, and a record whose `organizationClosedAt` does not read as a time is its own `context-invalid`.
- **Outbox**: events persisted once with a ULID and the next seq before they are sent; batches are the oldest stream's
  consecutive run of at most 1000, keep their `batchId` across retries of the same slice, back off from 30 s to an
  hour, and are consumed by the ack (`seq`, `held`, `rejected`). A 200 that acknowledges none of the batch is held and
  retried under the same `batchId`. A batch refused for what it is (400, 422, `device-mismatch`) is consumed as
  rejected and never retried; a 413 halves the batch size first; 409 `activation-closed`, 429, 5xx and no answer are
  retried. Past 10 000 pending events the oldest are dropped down to 9 000 and named in one `buffer.overflow`: a later
  drop joins the newest overflow not yet sent, so a burst ends in one event. Every `rejected[]` entry of an ack is
  consumed by its `eventId` whatever its reason, a reason the node does not know included (DEC-0100).
- **A purpose rejection** (DEC-0100, DEC-0102): an event rejected `purpose` (the receiver does not collect it for this
  person under the notice they acknowledged) holds its kind (`purposeHolds`, with the applied policy's `noticeVersion`
  and the `acknowledgedNoticeVersion` held at the rejection): no new event of that kind is collected and a pending one
  is named dropped in a `buffer.overflow`, never sent. The hold ends only when the applied policy names another
  `noticeVersion` than at the rejection, or a check-in response names an `acknowledgedNoticeVersion` other than the
  one held then; a value that becomes absent is no signal, and no timer ends it. `before-purpose` holds nothing. A
  kind is held only where a release signal can arrive (`purpose_holdable`): the enrollment binds a person (not
  `org-cloud`, not an unassigned device) and has received that person's `acknowledgedNoticeVersion` at least once.
  Until OE-06 applies the signed policy, its `noticeVersion` cannot be read (`notice_version` is None), so anywhere
  else a hold would drop the kind for good: there the rejection is consumed like `before-purpose`, the kind is still
  sent and refused until the organization's notice covers it, and the refusal is logged once per kind per day
  (`purpose-refused`). A `buffer.overflow` is never held, so a drop never writes another overflow. `org status`
  (`purposeHolds`: kind, since, the versions it waits to change; `org:purpose-held` in `degraded`), doctor and
  Health show every held kind. When OE-06 lands, the applied policy's `noticeVersion` becomes the second release
  signal and `purpose_holdable` widens to enrollments that only it can release.
- **Share registrations** (DEC-0086): derived by `share_registration.derive` (the contract's
  `deriveShareRegistrations`, all 51 derive cases) from `records/estate-record.json` and `records/delta-state.json`,
  which only `org records apply` writes after `verify_estate_record` and `apply_delta`. Sent only when the enrollment
  response lists `share-registration/1`; on change, after an enrollment, at least weekly; a typed refusal waits for the
  record to change; a 403 `purpose-not-collected` (DEC-0100, SR-5a: the person's acknowledged notice does not name a
  share registration's purposes) is `hold-notice`, held but not degraded, the sums kept, and sent again at the weekly
  resend, or at once when a check-in response names a new `acknowledgedNoticeVersion` (DEC-0102); a 410
  `organization-closed` is `stop` (DEC-0103, above); anything else (a held withdrawal included) retries with backoff. The answers are kept per grant.
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

Not built here: verifying and applying the server's signed policy and key sets (OE-06). Until then the check-in
reports the server layer at revision 0: `applied` while no response has carried a policy (the contract's word for
none arrived), and `failed`, its error naming the revision that arrived, once one has (`policy_report`, OBS-231), so
a server never reads a policy as in force on this machine; a check-in scheduler at the server's interval (the maintenance job runs hourly), and the per-context
`kid` and `iss` (CX-CON-06 in the contract). A settlement the meter accepts after its reservation expired lands after
its day was sent and is not counted (the meter does not refuse late settlements).

## The harness summary (OBS-192)

An organization sees what each enrolled machine runs (its target S8) and how that differs from what it expects (S10):
contract DEC-0101, corrected by DEC-0102 (`docs/specification/node-summary.md` there).

- **The port.** `observatory/engine/node/node_summary.py` ports `src/node-summary.ts` at the engine's contract pin:
  `harness_drift` (`harnessDrift`: one state per baseline item, in its order, the first of `missing`, `stopped`,
  `changed`, `outdated` that holds; a digest compared only for a skill or a plugin, a version only for a coding agent
  or a plugin), `version_below`, `resolve_policy`, `signed_policy_value` and `harness_baseline` (the baseline from the
  `mdm` and `server` layers only, never a person's `user` layer), and FAC-SEM-128 (`summary_findings`,
  `document_findings`: no credential, e-mail address or home path in any string, no item twice, `context` exactly
  `org:` and the certificate's `org.id`, drift exactly `harness_drift` under the baseline's revision and none without
  one). `tests/test_node_summary.py` runs it over the contract's eight semantic fixtures, the kit's own drift, version
  and baseline cases, and the schema over the positive and the eight negative fixtures (`fabric/node-vectors/summary/`,
  vendored by `tools/vendor_org_vectors.py` with the coding-agent catalogue, DEC-0093). DEC-0102's narrowing of a
  baseline is `baseline_problems` (a name in a summary's shape, a coding agent's a slug and a service's `id.instance`;
  `minVersion` only for a coding agent or a plugin, `digest` only for a skill or a plugin), held to the contract's nine
  negative baseline fixtures; the step that applies a signed policy (OE-06) refuses a policy it flags whole and keeps
  the revision it holds. DEC-0114 is `check_in_summary_findings` (the summary read with the check-in: FAC-SEM-128's
  clauses that need no policy, its device the check-in's) and `check_in_attribution_findings` (FAC-SEM-045 on the
  check-in's records against the client certificate: the summary's device and `org:<org.id>`, and the context of every
  credential request and call record), held to the contract's seven check-in fixtures.
- **The source.** The harness scan (`collectors/scan_harness.py`, OBS-61, OBS-88) adds `plugins` (each enabled and
  installed one by `name@marketplace`, its source and the version Claude Code recorded) and `coding_agents` (an agent of
  the catalogue is installed when its launch command is an executable in a bin folder of the scanned home, on `PATH`,
  or in Homebrew's and `/usr/local`'s; a generic command, `agent`, `cmd`, `cn`, `pi`, `omp` or `zed`, counts only when
  it resolves into the agent's own package or bundle; its version is read from where the launcher resolves, a
  `versions/<v>` folder, a Homebrew keg or cask, or its npm package's `package.json`, never by running it, and kept
  only when it is a release's version: dotted numbers with at most one prerelease word, `STRICT_VERSION`),
  `project_skills` (each project's own `.claude/skills` one level under the configured projects folder: name, content
  digest and every spelling of the project's folder as `~/<path>` inside the scanned home, none outside it; a project
  reached through a link is read once, by the folder it resolves to, with every spelling it was reached by kept; the
  folders are read only to judge the work folders below and never leave) and `codex_skills` (Codex's
  `~/.codex/skills` and the shared `~/.agents/skills`, a skill linked into both once: name, source `user`, content
  digest). The summary lists skills once by name, Claude Code's user-level and plugin skills first, then a project's
  (source `project`), then Codex's; the first read wins. The MCP scan (`collectors/scan_mcp.py`)
  gives the servers: a switched-off declaration is not loaded and not listed, the strongest verdict of a name's
  declarations is its health (`connected` ok, `needs-auth` degraded, `failed` down, anything unheard unknown), and a
  server Claude Code keeps per folder is `local`, its folder never sent.
- **How much it lists: `machine.mode` (DEC-0118, OBS-226).** `policy_scope(layers)` reads the effective
  `machine.mode` (`A` only from an `mdm` layer, anything else the default `C`) and the organization's own
  `workspace.folders` from the signed layers only (a person's `user` layer never widens what an organization is shown).
  Under `C` the summary lists the coding agents, the hook count, and only those MCP servers, skills and plugins that are
  installed in a folder matching `workspace.folders` (an MCP server Claude Code keeps for such a folder: each pattern
  segment matches one folder segment, `*` any run inside it, a folder inside a work folder counts; `folder_matches`;
  a project's own skill when any spelling of its project folder matches, so a linked project is admitted by the
  spelling that is a work folder) or named by the baseline with their kind, wherever it is installed: a baseline
  naming `x` also admits a project's skill `x` from a project outside the work folders (DEC-0118 admits what the
  baseline names). The person's own skill wins under either mode, as it does in Claude Code (a personal skill shadows
  a project's of the same name): under `C` a project's skill whose name the person's own skill holds is not listed,
  so the organization never reads a project's `x` where the agent loads the person's `x`, and the person's `x` is
  listed only when the baseline names it, the same row `A` reports (`_skills`). Everything else, installed for the person, machine-wide or for another
  context, is left out and counted per list in `outOfContext`, which stays here and is shown in `org summary`,
  `org status` and Health beside `mode`. No package names a context on this machine yet, so none is admitted as
  placed in it. Under `A` the summary lists the whole harness. Until the signed policy is applied (OE-06) no layer is
  held, so every summary is built under `C`: the organization sees the coding agents and the hook count.
  `tests/test_node_summary.py` (class `Scope`) holds it to the contract's `device-policy-organization.json` and
  `device-policy-mdm-managed.json` and its two refused folder lists.
- **Consent, per organization.** Under `C` an organization sees only its own context (DEC-0118, CT-92's first half);
  whether a person may withhold the summary altogether is still the contract's open question (CT-92's second half).
  Until it is answered the node takes the safest reversible choice: sending is OFF for each organization until the person runs
  `full org summary --org ID --consent on` (recorded in that enrollment's state with its time; `--consent off` stops
  it). `org status` and Health say `harness summary: not shared (consent off)` with that command; the dry run
  `full org summary --org ID` works before consent, so the person reads what they would share first. The preview
  without consent and the answer to `--consent` carry `notice` (`org.CONSENT_NOTICE`): names can carry a client's or
  a person's name, mode `A` sends every project's own skill names, and the exclusion list below is the guard.
- **The person's exclusion list.** `config/node-summary.json`, `{"exclude": [glob, ...]}`, owner-only: shell-style
  globs matched against each whole name (a coding agent's id, an MCP server, a skill, a plugin, a service key), applied
  before anything is checked or sent. What it keeps back is counted in `excluded`, which stays here. A list that is a
  link, not owner-only, in a folder others may write to, not a list of strings, or unreadable sends nothing
  (`org:node-summary-unavailable`, the reason worded by what failed), never an empty list; the folder's own parents
  are resolved, so a workspace under a linked folder such as `/tmp` reads it.
- **What leaves.** `build` copies an allow-list of fields into the schema's shape and checks the result with
  FAC-SEM-128 before it hands it over (`SummaryRefused` otherwise): names, catalogue ids, versions, digests, sources,
  transports, health and a hook count. A name the schema refuses (a space, a slash, a dotted `@` suffix) is left out and
  counted in `omitted`, which stays on this machine. So is a name holding a home folder (`hides_home`): the contract's
  `hasHomePath`, which since DEC-0105 catches the dashed `-Users-<x>` or `-home-<x>` after any character that is not
  an ASCII letter or digit (`notes.-Users-alice-x`, `x@-home-alice`), and the node's own remaining check for the one
  shape the contract leaves to a reader, a dashed home glued to a letter or digit and continued by two more dash
  segments as a flattened path continues (`a-Users-alice-projects-x`, `DASHED_HOME`); `smart-home-mcp`,
  `end-users-guide`, `my-home-page` and `Users-guide` pass both. Drift is capped at the schema's 1,024 items; the count
  past it stays here.
- **When.** `Enrollment.check_in` puts the summary in `records.nodeSummary` only when the server's latest protocols
  list `node-summary/1`, the person consented, and its content digest (everything but `sequence` and `at`) differs from
  the last one the server took or that one is a day old. Before it leaves it is judged with the check-in and against
  this enrollment's certificate (DEC-0114, above); one that fails stays here with `nodeSummaryNote` reason
  `attribution`. The check-in's own `protocols` lists only what the node reads, `fabric-device/0.1`
  (`NODE_READS`): a server gates on it what it sends (DEC-0116, DEC-0117), so no server sends this node
  `records.repairs` (`repair-request/1` joins the list only while a repair could run here, OBS-193, below), credential decisions,
  envelopes, revocations or `callRecordsAccepted`; and the node sends none of DEC-0111's members (`credentialRequests`,
  `callRecords`), whatever the server lists, until the credential exchange (OBS-191, OBS-194). Its `sequence` is spent once sent, answered or not, like the
  check-in's `sequenceNum`; a failed check-in leaves the summary due. The scans are read before the enrollment's lock is
  taken (they can be large; at most 32 MB each), and a tick reads them only when a check-in is due.
- **Across a renewal or a re-enrollment** (both write the enrollment's state afresh): the sequence keeps rising
  (DEC-0101: per device and context; a server may refuse one that goes back); a renewal by the key on record keeps
  the consent, the last summary taken (its digest) and a refusal still waited out, so it changes nothing the person
  sees. A new token's enrollment never carries the consent (the token may enroll another person), and a removal or an
  organization closed clears it, so the summary is sent again only after the person consents again (OBS-221,
  `_summary_carried`).
- **Only current scans.** `read_harness_scans` refuses a scan whose integration is off (a file left from before is not
  sent), one older than 48 hours by its own time, one without a time, and a harness scan written before this change
  (one of `plugins`, `coding_agents`, `project_skills` or `codex_skills` missing, `org.HARNESS_SUMMARY_LISTS`):
  nothing is sent, the reason is kept (`nodeSummaryNote`) and `org status` says
  `org:node-summary-unavailable`. A summary built from a missing, stale or partial scan would tell the organization the
  machine runs nothing of that kind, or what it ran days ago. A fresh MCP scan is not fresh health: the tick's scan
  reads declarations only and carries the last on-demand probe's verdicts for up to seven days under a new
  `scanned_at`, so each server's health is judged by its own probe time (`liveness_at`), and a verdict probed more
  than 48 hours before the summary's `at`, or with no probe time, is sent as `unknown`, never as an old `ok` or `down`
  (OBS-221, `node_summary._fresh_health`).
- **A server that refuses it never stops check-ins.** A check-in carrying a summary that the server refuses in a way
  the summary may cause (400, 413 or 422 whatever it says, or another client error whose code, pointer or message names
  the summary or the records; never a signature, certificate or rate refusal) is sent again at once without it. When
  that one is taken, the refusal is recorded (`summaryRefused`: when, status, code, retry time) and the summary waits a
  day, so heartbeats, rotation and renewal go on; `org status` says `org:node-summary-refused` and Health when it is
  tried again. A refusal that the bare check-in gets too is the check-in's, not the summary's: the summary is then held
  until a check-in is taken, so a refusing server gets one request per check-in, not two.
- **Shown.** `org summary --org <id>` prints the next summary as it would leave, saving and sending nothing.
  `org status` (`nodeSummary` with its `state`: `not-listed`, `consent-off`, `unavailable`, `refused`, `pending` or
  `sent`; the first, third and fourth also as `org:node-summary-*` in `degraded`), doctor and Health read the saved
  state (`summary_notice`): whether the server reads it, the consent, when it last took one, how much it listed, and
  the drift. Beside a plugin's drift that reads `changed`, `driftCauses` (`node_summary.drift_causes`, also in the
  preview) and Health say `this node sends no plugin digest; the contract does not say what it covers`: the cause is
  this node's, not the machine's (below).

Not built here: the drift reaches an organization only once the node applies its signed policy (OE-06): until then
`policy_layers` is empty, no baseline applies, and the summary carries no drift, as the contract asks of a summary
under a policy without one. The agent services of the summary's context: a Fabric service descriptor names no context
and no agent reference an estate placement could match, so `services_in_context` is empty rather than guessed, and a
person's services in other contexts are never listed (DEC-0082; the operator's question is the contract's CT-92). At
the pin (`de8cec30`) the contract still names no context in a service descriptor (`docs/specification/contexts.md`:
"a service descriptor carries none"; `schemas/service-descriptor.schema.json` has no context member), and an estate
placement names an `agentRef`, which no descriptor carries, so there is nothing to place a service by. A plugin's
digest: the contract defines the field (`docs/specification/node-summary.md` line 28 and the schema's
`plugins[].digest`) but not what it covers, unlike a skill's ("SHA-256 of the skill's files as installed"), and no
vector computes one, so the node sends none rather than one a server could not reproduce. The cost: the drift rule
(`node-summary.md` line 75, `changed` when the item "carries another, or none") makes every plugin whose baseline
item sets a digest read `changed` for as long as that holds, which is why the cause is shown beside it (above). The
question is with the contract (OBS-275).

## The repair runner (OBS-193)

An organization's administrator asks one enrolled machine to run one package (its target S10): contract DEC-0101,
corrected by DEC-0102 and gated by DEC-0117 (`docs/specification/node-summary.md#repairs` there), under the machine's
remote-execution policy (FAC-SEM-068).

- **The port.** `observatory/engine/node/repair.py` ports `repairDecision` (`src/node-summary.ts`), the settings
  canonical form (`settingsCanonicalJson`, `policySigningInput`), `verifyWith` (Ed25519 of full order, or ES256 over a
  raw SEC 1 point with an R || S signature) and FAC-SEM-068's remote-execution answer (`remote_execution_allowed`) at
  the engine's pin. `tests/test_node_repair.py` runs it over every case of the contract's shared vectors
  (`fabric/node-vectors/repair/repair-decision.json`), the settings form over `canonical.json`, the request and result
  shapes (`request_problems`, `result_problems`) against the vendored schemas and every repair fixture, and the
  FAC-SEM-068 remote-execution case. A key's window is `relay.key_window_holds` (DEC-0115, DEC-0119); every time is
  read as RFC 3339 strictly.
- **Two readings the contract leaves to the port, both the safe side.** A signed layer whose `expiresAt` does not read
  as a time is read as expired (the reference's `Date.parse` would make it never expire; the schema admits no such
  layer), so remote execution stops. A request the schema refuses (a two-line `task`, a member it does not know) is
  never shown or run and is answered `refused` `bad-signature`, the one reason for a request whose signed form is not
  the contract's; one whose `id` does not read is recorded in the journal and not answered (a result needs an id).
- **What a hostile server sends stops nothing** (the security review of #366). A request deeper than 8 levels,
  larger than about 64 KB, or holding a lone surrogate (JSON `"\ud800"`, which no UTF-8 store or canonical form holds)
  is refused before anything else reads it (`body_problem`), with that code as its note; any other failure on one
  request is journalled by its exception's name (`repair-unreadable`), never its text, and the others go on. The
  check-in's own state (the next check-in time, the protocols, an arrived policy) is saved before any repair is read,
  so nothing a repair holds loses it.
- **No shell, no command.** A request that passes runs only as a local action the machine's owner allow-listed for its
  `agentRef` in the owner-only `config/node-repair.json` (`{schemaVersion: 1, agents: {agentRef: {action, targetId}},
  lifecycle: {bearerSlot}}`): `restart` or `ensure_running` of a target enrolled with `node.lifecycle`, asked of the
  broker through its own door (`POST /mcp` on the local server) with the bearer of an access binding the operator
  issued for the runner, read from the vault slot for one call (`LifecycleExecutor`). The broker's grant, its person
  rules (`in_use_by_person`, `held_off`) and its receipts hold as for any caller; the lifecycle request id is derived
  from the organization and the repair id, so a start retried after a crash gets the first operation. A package the
  policy allows but the owner allow-listed nothing for is answered `failed` (`run-failed`) before the person is told,
  since nothing would run. A file that is a link, not owner-only, or not that shape runs nothing, with the reason.
- **The person first.** A request that passes everything but the person step is shown at once (`org status`
  `repairs`, `org repair`, doctor and Health: the organization's one-line `task`, the package, when it starts, and
  `full org repair --org ID --decline REPAIR_ID`) and starts no earlier than `NOTICE_SECONDS` (15 minutes, one pass of
  the maintenance job) later; a decline answers it `person-declined` at once. The decision is taken again at the
  moment it would start, with the time the person was told, so a request or a policy that expired meanwhile is refused.
  A machine with role `org-cloud` binds no person: the signed request is its notice and `notifiedAt` the moment the
  node verified it (DEC-0102), and it starts at once. The role comes from the server's binding, which no signature
  covers, so it counts only when the certificate binds no person and the enrollment this machine made with a token
  carried it (`org_cloud`, `enrolledRole`); a certificate binding a person with role `org-cloud` is refused, and a
  renewal or a rotation that adds, drops or changes a role is refused `binding-changed` (`_same_binding`), the
  certificate on record kept. A certificate without a role is recorded `role: null`; a record saved by 0.27.0 or
  0.28.0, which kept no role at all, renews and rotates as before and, with no `enrolledRole`, keeps the notice.
- **Once per id, every step audited.** One row per request id in the enrollment's owner-only outbox database
  (`repairs`): the request as received, when it was received and the person told, the lifecycle operation, and the
  one stored `repair-result/1` (checked against its schema before it is stored). A redelivered id gets that result
  again unchanged and never runs twice; `already-answered` is never sent. A body is kept only while a schema-valid
  repair waits or runs; a refused or answered one keeps its id and result alone. An answered id is forgotten only
  after a server took its result and its own request's `expiresAt` is a day past (a redelivery is refused `expired`
  by then), never by when it was answered: a request that came before its `issuedAt` is answered early and stays live.
  A row whose request had no readable expiry is kept, within the row bound. At most
  64 repairs wait and 10 000 rows are kept: past them a request is not recorded (`repair-book-full`) and the server
  sends it again later. A removal or a closed organization forgets what had not started. Every step writes one journal line of codes and ids
  (`repair-received`, `repair-notified`, `repair-declined`, `repair-started`, `repair-answered`,
  `repair-redelivered`, `repair-malformed`, `repair-unreadable`, `repair-book-full`, `repair-unasked`): never the bearer, the signature, the task text or a body.
- **The wire.** A check-in response's `records.repairs` (at most 16) is read after the check-in is taken, and only
  when that check-in listed `repair-request/1`: repairs it did not ask for are counted in the journal
  (`repair-unasked`) and dropped, never recorded or run. Each request is
  recorded and answered or shown under the enrollment's lock; the tick's `repairs` step, after the check-in, starts
  what is due and polls what runs (a run past 30 minutes is `failed`, `timeout`). Results ride the next check-in's
  `records.repairResults` (at most 64) only to a server whose response lists `repair-result/1` (DEC-0117); a check-in
  refused for what they may cause is sent again at once without them and they wait a day (`org:repair-results-refused`),
  so heartbeats never stop. The check-in lists `repair-request/1` in its own `protocols` only while a repair could
  run here (`Enrollment.repairs_enabled`): the signed key set and policy applied, `repair.packages` listing a package,
  remote execution not stopped, and the owner's allow-list naming an action and the bearer slot. DEC-0117 lists it
  when the node "reads repairs and acts on them", and a node that would refuse every repair does not ask for them.

Not built here: the signed key set and policy are applied by OE-06, so `policy_keys` and `policy_layers` are empty
and `repairs_enabled` answers `policy-not-applied`: the check-in does not list `repair-request/1`, and a repair a
server sends anyway is counted in the journal and dropped. Until
then the runner is exercised end to end only in tests, with the policy injected (`tests/test_node_repair.py`
`Enrolled`). The person is told through `org status`, doctor and Health, not a system notification. A package runs
only as a lifecycle action: a repair agent of its own, verified by its digest and run in a sandbox, is a later step.
The bearer goes to whatever listens on the engine server's loopback port, which has no proof of its identity yet
(OBS-248).

## Lifecycle (OBS-79)

The node wakes, stops and restarts enrolled desktop apps and background services for agents. Its plan, file by file
and step by step, is [LIFECYCLE.md](LIFECYCLE.md). Steps L1 to L3 are merged and released in 0.29.0: L1,
`observatory/engine/node/lifecycle/broker.py`, the platform-neutral broker (the closed registry, the authority, durable
operations and receipts, the effect journal, circuits, the restart budget, the idle and takeover rules), tested against
fake adapters by `tests/test_node_lifecycle_broker.py` (#342); L2, the macOS adapter `node/lifecycle/macos.py` and its
Swift helper compiled for a source install (`full lifecycle build-helper`, #344); L3, the six `lifecycle.*` tools on
the local server behind access bindings, the well-known `node.lifecycle` entry and `full lifecycle` (#347). L4, the
supervisor (`node/lifecycle/supervisor.py`: the broker's pass on its cadence inside the local server, the watcher's
events fed to the broker, the store pruned on the sweep), is built for 0.30.0. The helper's release build inside the
signed app (L2r) and the operator's import with the identity migration (L5) follow in 0.30.0.

## What comes next

| Slice | What |
|---|---|
| 5 | the ceremony surface: built against the contract fixtures (OBS-197, above); live acceptance when the hub's side (Fabric CO-319 to CO-321) runs the rendezvous and the passkey page |
| 6 | the node service under the OS supervisor (`keep_connected` with the applied estate record), after slice 5; it runs inside the local server, which is the node ([MACHINE-UTILITY.md](MACHINE-UTILITY.md) D1) |
