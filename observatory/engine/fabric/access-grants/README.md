# Access grants (vendored, DEC-0187)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit `320c7ab9b8397cb392ca5f3ac4d56dcc5c7786a1`
(DEC-0187, the contract's CT-350, its pull request #345), never from a branch. SOURCE: that commit's
`schemas/access-grants.schema.json`. The contract's positive and negative fixtures are not copied: they name an
example agent the public-release privacy gate refuses, so `tests/test_access_grants.py` builds its own cases. The
commit is not the engine's contract pin (`fabric-contract.lock.json`): the pin moves with its own review, so this
folder is listed in `contract_lock.PIN_CHECK_EXCLUDES` and `contract_lock.VENDORED_AT_OTHER_COMMITS` (OBS-658).

`access_grants.py` ports `accessGrantsRules` (FAC-SEM-185) and `grantState` from the contract's
`src/access-grants-rules.ts` at that commit; `tests/test_access_grants.py` runs the contract's own cases through them.

Do not edit these files here. When the contract changes, copy them again from a new commit and update the commit and
the digests below; `tests/test_access_grants.py` checks the digests.

| File | sha256 |
|---|---|
| `access-grants.schema.json` | `409c29d651074c7ca7790e7474434c987e409d6908694dc62812055abc1a6aa1` |
