# fabric-delegation/0.1 and grant-check schemas (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit
`8e49fbd` (merge of PR #30, DEC-0039: the delegation token, the meter's onward token, the grant-check request and
the `window` of a `limit-reached` denial) (`schemas/`). They are normative for what the meter answers
(`grant-check.schema.json`), what it accepts (`grant-check-request.schema.json`), the token's claims
(`delegation-token.schema.json`), the estate's key set (`delegation-jwks.schema.json`) and the standing grant the
hub projects into its grant directory (`governance.schema.json`). `tests/test_meter.py` validates the meter's
answers against them and checks the digests below.

Known gap: contract DEC-0041 (reserved by Fabric on 2026-10-11, PR to follow) adds `call` to the denial's
`window` enum for a `perCallMaxUsd` refusal and an optional `task` claim to the token. The meter already answers
`window: call` (Fabric ADR-0144 amendment 1); `tests/test_meter.py` validates that one answer against this
schema with `call` added, until the files are copied again from DEC-0041's commit.

Do not edit them here. When the contract changes, copy the files again from the new commit and update the commit
and the digests below.

| File | sha256 |
|---|---|
| `common.schema.json` | `a00baa59c8b7a236a40b7dca659f99e8aa26e9265fdd9fd6b325f8c3714412ca` |
| `delegation-jwks.schema.json` | `24354d5653e11bf6df61eb3ebe5b690a737c777fe44a473dffdb9e349d1a2c2e` |
| `delegation-token.schema.json` | `74605fbb7ada1b6824c7f923c32e067dd9e9bfa534b977f3e7fbe5cdf4cd69c3` |
| `governance.schema.json` | `6adc932b5e82e078ad43e9383a1333e9e6871312f14e69e1aea0b9ba6d6d5480` |
| `grant-check-request.schema.json` | `5ceba8fae472ad59f5d4d653401a5ca236c62cf4a96a182a1796d7966824e63a` |
| `grant-check.schema.json` | `2803f990a6ab09a406b6c4c79a60e188567eed78f948a7c4e3c567309b0117c1` |
