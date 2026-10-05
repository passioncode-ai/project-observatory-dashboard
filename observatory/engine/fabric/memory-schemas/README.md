# memory/0.1 schema (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit
`aa2f0977bbbb32e9940e16b2654af84960abfb54` (`schemas/`), the merge of DEC-0023: agent memory
on the wire, first served by Project Observatory. `common.schema.json` comes with it because
the memory schema refers to its `timestamp`; it is that commit's copy, which differs from the
one `fabric/interop-schemas/` pins.

The schema is the contract's, not the engine's. `mcp/memory_wire.py` validates every
`memory.*` call and answer against it, and `fabric/memory-provider.json` is this provider's
declaration under it. Do not edit the copy here. When the contract changes, copy the file
again from the new commit and update the commit and the digest below;
`tests/test_memory_wire.py` checks both.

| File | sha256 |
|---|---|
| `memory-capability.schema.json` | `f404a079519632c255e5dc9ad72accac42b6fd97517c625bf88bdcf0ea6df733` |
| `common.schema.json` | `8c7c781e2b798a549eaf19175740f5873aa3d51518e3316ee35177c79630209a` |
