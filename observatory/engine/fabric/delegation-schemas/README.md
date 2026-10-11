# fabric-delegation/0.1 and grant-check schemas (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit
`7931c1d` (merge of PR #32, DEC-0041 over DEC-0039: the delegation token with its `task` claim, the meter's onward
token, the grant-check request, and the `window` of a `limit-reached` denial including `call`) (`schemas/`). They are normative for what the meter answers
(`grant-check.schema.json`), what it accepts (`grant-check-request.schema.json`), the token's claims
(`delegation-token.schema.json`), the estate's key set (`delegation-jwks.schema.json`) and the standing grant the
hub projects into its grant directory (`governance.schema.json`). `tests/test_meter.py` validates the meter's
answers against them and checks the digests below.

Do not edit them here. When the contract changes, copy the files again from the new commit and update the commit
and the digests below.

| File | sha256 |
|---|---|
| `common.schema.json` | `a00baa59c8b7a236a40b7dca659f99e8aa26e9265fdd9fd6b325f8c3714412ca` |
| `delegation-jwks.schema.json` | `24354d5653e11bf6df61eb3ebe5b690a737c777fe44a473dffdb9e349d1a2c2e` |
| `delegation-token.schema.json` | `2fb1d2794f12e2fc85d5b9f900e074860262af1965ab4c44f19cb0047bb099a7` |
| `governance.schema.json` | `6adc932b5e82e078ad43e9383a1333e9e6871312f14e69e1aea0b9ba6d6d5480` |
| `grant-check-request.schema.json` | `5ceba8fae472ad59f5d4d653401a5ca236c62cf4a96a182a1796d7966824e63a` |
| `grant-check.schema.json` | `d18763d5a1ced6dbca074d347bf0c152d978db50738555c2a02806820f75ab1a` |
