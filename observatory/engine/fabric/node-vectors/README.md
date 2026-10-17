# fabric-relay/0.1 node vectors (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit
`bf3fd6b2a9387ee501ef635de4e26524c3ea2b4f` (`fixtures/vectors/`). They are the contract kit's own vectors for RFC 8785
(`canonical.json`), P-256 key validation (`p256.json`) and fabric-relay/0.1 (`relay.json`, DEC-0047, DEC-0066), written
by the contract's generators, never by hand, plus identifier equality (`identifiers.json`, DEC-0076), the estate record
(`estate-record.json`, DEC-0055) the revocation delta with its heartbeat (`revocation-delta.json`, DEC-0054) and the secret envelope
(`secret-envelope.json`, DEC-0047 item 7).
`tests/test_node_foundation.py`, `test_node_estate.py`, `test_node_revocation.py` and `test_node_envelope.py` run the node's ports over every
case; the first checks the digests below.

Do not edit them here. When the contract changes, copy the files again from the new commit and update the commit and
the digests below.

| File | sha256 |
|---|---|
| `canonical.json` | `e63411c0e1b615773d20c8c82b34a0c5ef7fc367fffc04d106b537c2628e5663` |
| `estate-record.json` | `3c37bf7bf205fe14674954200ef14b8e733e70a7e350ef08293da3edee2f61c0` |
| `identifiers.json` | `ff8726a99db6475e3a28f14bead020118ba01a1ac5844d3fd62678e3d9369f8b` |
| `p256.json` | `8dbfdb7cf8f80dcda4bfcfd7c577860db6d4d18ad2d03975beef38df80c0313b` |
| `relay.json` | `6242729104e3fb4f08e144194a94af40b9bb3df15045bba733cf985de281fb3f` |
| `revocation-delta.json` | `07aa63ef6293fc5c1f355f21db7b6097e62e512d31bfcf837691d7953c51d3ec` |
| `secret-envelope.json` | `446776b8a49a69d1b772265450cf3b6bdb1e7a0e9da7945e154b710d857c5ead` |
