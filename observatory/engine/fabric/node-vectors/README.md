# fabric-relay/0.1 node vectors (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit
`bf3fd6b2a9387ee501ef635de4e26524c3ea2b4f` (`fixtures/vectors/`). They are the contract kit's own vectors for RFC 8785
(`canonical.json`), P-256 key validation (`p256.json`) and fabric-relay/0.1 (`relay.json`, DEC-0047, DEC-0066), written
by the contract's generators, never by hand. `tests/test_node_foundation.py` runs the node's ports (`node/jcs.py`,
`node/p256.py`, `node/relay.py`) over every case and checks the digests below.

Do not edit them here. When the contract changes, copy the files again from the new commit and update the commit and
the digests below.

| File | sha256 |
|---|---|
| `canonical.json` | `e63411c0e1b615773d20c8c82b34a0c5ef7fc367fffc04d106b537c2628e5663` |
| `p256.json` | `8dbfdb7cf8f80dcda4bfcfd7c577860db6d4d18ad2d03975beef38df80c0313b` |
| `relay.json` | `6242729104e3fb4f08e144194a94af40b9bb3df15045bba733cf985de281fb3f` |
