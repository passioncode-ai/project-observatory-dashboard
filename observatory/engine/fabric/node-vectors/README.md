# fabric-relay/0.1 node vectors (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit
`1f31e5afa2cb2444ff44479de0c3f89fdbca76ff`, the engine's contract pin (`observatory/engine/fabric-contract.lock.json`),
every file here: `fixtures/vectors/`, `schemas/`, and the `estate-*` fixtures of `fixtures/semantic/` in `semantic/`, all
but one: `estate-secret-value-as-slot.json` names the AWS documentation's example access key id as a slot, a shape the
public-release gate refuses, so `test_node_pairing.py` rebuilds it from `estate-record-ok.json`. They are the contract
kit's own vectors for RFC 8785 (`canonical.json`), P-256 key validation (`p256.json`) and fabric-relay/0.1
(`relay.json`, DEC-0047, DEC-0066), written by the contract's generators, never by hand, plus identifier equality
(`identifiers.json`, DEC-0076), the estate record (`estate-record.json`, DEC-0055) the revocation delta with its
heartbeat (`revocation-delta.json`, DEC-0054) and the secret envelope (`secret-envelope.json`, DEC-0047 item 7).

The estate ceremonies (DEC-0084, contract pull request #113): `enroll-token.json` (the headless join), `pairing.json`
(commit, reveal and the comparison code), and the DEC-0084 revisions of `relay.json` (the admission set and open
sessions), `revocation-delta.json` (a device-key reference) and `secret-envelope.json` (revocations of verified
deltas). The partner channel (DEC-0090, contract pull request #130, OBS-171): the five `estate-partner-*` fixtures of
`semantic/` (FAC-SEM-122, a partner's publisher key trusted only in the organization contexts `publisherScopes[]`
names).

`tests/test_node_foundation.py`, `test_node_estate.py`, `test_node_revocation.py`, `test_node_envelope.py`,
`test_node_pairing.py` and `test_node_enroll.py` run the node's ports over every
case; the first checks the digests below.

`http-signature.json` (the device transport binding, DEC-0056), `share-registration.json` (DEC-0086) and
`device-enrollment.schema.json` (DEC-0031, DEC-0094), with the device fixtures in `device/` (`fixtures/positive/` and
`fixtures/negative/`), are copied byte for byte (OBS-161 pin) from the contract at commit
`1f31e5afa2cb2444ff44479de0c3f89fdbca76ff` (`fixtures/vectors/`, `schemas/`); `tests/test_node_org.py` runs the
receiver's check over every transport case, the node's derivation and sender gate over every `derive`, `grantId` and
`gate` case, and its enrollment request against the schema's `key` rules. One command re-vendors the whole folder at
another commit, removes an `estate-*` fixture the commit no longer has, and rewrites every commit and the digests here:
`python3 tools/vendor_org_vectors.py --contract <checkout> --commit <sha>`.

Do not edit them here. When the contract changes, copy the files again from the new commit and update the commit and
the digests below.

| File | sha256 |
|---|---|
| `canonical.json` | `e63411c0e1b615773d20c8c82b34a0c5ef7fc367fffc04d106b537c2628e5663` |
| `enroll-token.json` | `475e8e54cb47ceeb8b7b478327b7f8b9e89cf8293521986c65a9f3ad437bbc71` |
| `estate-record.json` | `3c37bf7bf205fe14674954200ef14b8e733e70a7e350ef08293da3edee2f61c0` |
| `http-signature.json` | `baaedfde6b6edcc9155fe7cf739f3173ccbbd1404aa96064688f36be34d393f5` |
| `identifiers.json` | `ff8726a99db6475e3a28f14bead020118ba01a1ac5844d3fd62678e3d9369f8b` |
| `p256.json` | `8dbfdb7cf8f80dcda4bfcfd7c577860db6d4d18ad2d03975beef38df80c0313b` |
| `pairing.json` | `c5d99e2a54ba210a06b94350bebda635369bf50b470981c6d10aedbf914c498b` |
| `relay.json` | `47563a359dc876500bb0405689eaa8c43bd9e148ab09c77c1a0f74e5850d42bb` |
| `revocation-delta.json` | `cdb1fb989808bf0bc7843ef39571c1aa2a27d2f2dd2281572e921fc77bfb7217` |
| `share-registration.json` | `20bd5c80462facc9e34ca557933cbf43d0098abefa50b5cf06e8aeb833b89733` |
| `secret-envelope.json` | `3ed14f7f95857b10a8d7ec97ded697ea65ea564c9f4c731e11c221e9ef03a3b3` |
| `device-enrollment.schema.json` | `db895afa14641d93d2f677b2a02b0ac762ba3f10f57a14de5171018ac05893a5` |
| `device/device-enrollment-request-file-key.json` | `5ff4d130ad4ad406d730eb7b353581f9ab0058e4a5ecb2a674bda8a9c64a9091` |
| `device/device-enrollment-file-key-not-exportable.json` | `fd07f7ec3be7be88cfe175524a8d08afea88bf31f1136ff25fa492a94e69dbdc` |
| `device/device-enrollment-file-key-attested.json` | `27fec7d9be6b86f8e99b152c7025f6b9046006e77d949ae401ddf2df537e7a27` |
| `device/device-enrollment-hardware-key-exportable.json` | `b3641681253c75eb971e23b245187f3b16c8dad674d69834d8e9b13134e5ad64` |
| `device/device-check-in-response-protocols.json` | `f7d9a64a109c118d86b15d7b309fc880d595d4e2ea4d897add884d94b7ce6d41` |
| `device/device-enrollment-request-key-on-record.json` | `67932d96183358f4f977bdbef25b90159cea179fc9f3d9f0246ce15968b216c2` |
| `device/device-enrollment-key-on-record-with-token.json` | `df17dd6bb544c7f53fb7bb4be94bcec531daf4e9a9bcc3775745259d65e9b982` |
