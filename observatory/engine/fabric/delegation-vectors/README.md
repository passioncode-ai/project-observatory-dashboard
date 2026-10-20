# fabric-delegation/0.1 verifier vectors, DEC-0045 (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit
`07042181bfae2a481aa0da1a5651f24c76669c48` (`fixtures/`). They are the contract's own inputs for its
`test/delegation-sealed.test.ts` (DEC-0045, "Delegation sealed"), which `tests/test_delegation_sealed.py` ports case
by case to `delegation.py` (OBS-162):

- `delegation-small-order.json` (`fixtures/vectors/`): a delegation token **forged without any private key** under a
  small-order public key (an all-zero `x`) published in the key set (CT-29). It is a public test vector, not a
  credential: nothing in it signs anything a real verifier should accept, and a verifier that answers `ok` for it is
  forgeable. A secret scanner may flag its token by shape; it is reviewed and stays here, never in a log.
- `delegation-token-claims.json` (`fixtures/positive/`): the valid claims every case varies.
- `delegation-mint-meter-ok.json`, `delegation-mint-meter-depth-five.json` (`fixtures/semantic/`): the meter's own
  onward token, and one whose chain holds five actors (DL-6: only the meter's hop may).
- `delegation-token-chain-too-deep.json`, `delegation-token-two-grants.json` (`fixtures/negative/`).

Do not edit them here. When the contract changes, copy the files again from the new commit and update the commit and
the digests below; `tests/test_delegation_sealed.py` checks them.

| File | sha256 |
|---|---|
| `delegation-mint-meter-depth-five.json` | `926e813019b43dce95e010c4af0c6b1671cd8c84339f629d3a4b8ca97e24824d` |
| `delegation-mint-meter-ok.json` | `36968e9e7edb3408522e606e3cc1943d3c0c1fc3400114e3e7829f002977b46a` |
| `delegation-small-order.json` | `f7ebb693d512f68d10efe5f2c89d02f599fe883f35bf080d7c5d1d411c02b808` |
| `delegation-token-chain-too-deep.json` | `e2a80f1e116282abd494d70e36133e95a08ddae9261e8bf3db4c61477a094904` |
| `delegation-token-claims.json` | `3fad33104234067e8e8e41776820b30b1cd9404ffb282c5d30ee377130a8af00` |
| `delegation-token-two-grants.json` | `9dee5cf5cffe2b2ff05446d7a2e3a81c00bddf38f7f37c8e103f983bd09be2cf` |
