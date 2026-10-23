# fabric-service/0.1 schemas (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit `7931c1d` (`schemas/`; DEC-0017, and DEC-0021 for `service-usage.schema.json`)
They are normative for the descriptor, the well-known document, the events page and the usage report;
`tests/test_fabric_service.py` validates what the always-on server produces against them and checks the digests below.

**Not at the contract pin, on purpose (OBS-205).** At the pin, `service-descriptor.schema.json` names a word the
maintainer's private identifier list holds, so the public-release privacy gate refuses the copy until that word is
reviewed for `tools/public-identifiers.json` (row OBS-212). Until then the folder stays at this commit: the engine
passes it to the contract's `pin:check` as `--exclude fabric/service-schemas`, and `contract_lock.PIN_CHECK_EXCLUDES`
and `contract_lock.VENDORED_AT_OTHER_COMMITS` say why. Once it moves, `tools/vendor_contract_schemas.py` vendors it at
the pin with the `$ref` closure (at the pin the well-known document also refers to `model-profile.schema.json` and
`registry-models.schema.json`).

Do not edit them here. When the contract changes, copy the files again from the new commit and update the commit and
the digests below.

| File | sha256 |
|---|---|
| `common.schema.json` | `a00baa59c8b7a236a40b7dca659f99e8aa26e9265fdd9fd6b325f8c3714412ca` |
| `service-common.schema.json` | `adc1e3e59d7762e8883d3aef067d5a64a700de156600b6606654c557f35187d2` |
| `service-descriptor.schema.json` | `1ba8d0edad5a5fea684b2174184f3e88ab56320004a6f3682bec09eea96e1652` |
| `service-events-page.schema.json` | `9983a5444e6ab7314a413014588482a164761bf4c1670e26c8db1e3c43cdcb94` |
| `service-usage.schema.json` | `fed51d4a57cd1946f4ceb04f86175eee0911afc96997535c5cdb8e9a7cfba380` |
| `service-well-known.schema.json` | `2b34a44d6d385a708e4382f47f5f50a1414e1486e395578dc99057ba84de4b68` |
