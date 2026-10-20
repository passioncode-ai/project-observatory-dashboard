# fabric-relay/0.1 node vectors (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit
`1531f21014171f5168fba43a504276e487a7b363` (`fixtures/vectors/`, and the `estate-*` fixtures of `fixtures/semantic/` in `semantic/`, 69 of 70: `estate-secret-value-as-slot.json` names the AWS documentation's example access key id as a slot, a shape the public-release gate refuses, so `test_node_pairing.py` rebuilds it from `estate-record-ok.json`). They are the contract kit's own vectors for RFC 8785
(`canonical.json`), P-256 key validation (`p256.json`) and fabric-relay/0.1 (`relay.json`, DEC-0047, DEC-0066), written
by the contract's generators, never by hand, plus identifier equality (`identifiers.json`, DEC-0076), the estate record
(`estate-record.json`, DEC-0055) the revocation delta with its heartbeat (`revocation-delta.json`, DEC-0054) and the secret envelope
(`secret-envelope.json`, DEC-0047 item 7).

The same commit (the merge of contract pull request #113, DEC-0084) carries the estate ceremonies: `enroll-token.json`
(the headless join), `pairing.json` (commit, reveal and the comparison code), and the DEC-0084 revisions of
`relay.json` (the admission set and open sessions), `revocation-delta.json` (a device-key reference) and
`secret-envelope.json` (revocations of verified deltas).

`tests/test_node_foundation.py`, `test_node_estate.py`, `test_node_revocation.py`, `test_node_envelope.py`,
`test_node_pairing.py` and `test_node_enroll.py` run the node's ports over every
case; the first checks the digests below.

Do not edit them here. When the contract changes, copy the files again from the new commit and update the commit and
the digests below.

| File | sha256 |
|---|---|
| `canonical.json` | `e63411c0e1b615773d20c8c82b34a0c5ef7fc367fffc04d106b537c2628e5663` |
| `enroll-token.json` | `475e8e54cb47ceeb8b7b478327b7f8b9e89cf8293521986c65a9f3ad437bbc71` |
| `estate-record.json` | `3c37bf7bf205fe14674954200ef14b8e733e70a7e350ef08293da3edee2f61c0` |
| `identifiers.json` | `ff8726a99db6475e3a28f14bead020118ba01a1ac5844d3fd62678e3d9369f8b` |
| `p256.json` | `8dbfdb7cf8f80dcda4bfcfd7c577860db6d4d18ad2d03975beef38df80c0313b` |
| `pairing.json` | `c5d99e2a54ba210a06b94350bebda635369bf50b470981c6d10aedbf914c498b` |
| `relay.json` | `47563a359dc876500bb0405689eaa8c43bd9e148ab09c77c1a0f74e5850d42bb` |
| `revocation-delta.json` | `cdb1fb989808bf0bc7843ef39571c1aa2a27d2f2dd2281572e921fc77bfb7217` |
| `secret-envelope.json` | `3ed14f7f95857b10a8d7ec97ded697ea65ea564c9f4c731e11c221e9ef03a3b3` |
