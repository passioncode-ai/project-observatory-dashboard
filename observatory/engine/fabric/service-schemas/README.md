# fabric-service/0.1 schemas (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit `1f31e5afa2cb2444ff44479de0c3f89fdbca76ff` (the contract pin, `schemas/`)
by `tools/vendor_contract_schemas.py`, which takes the commit from `fabric-contract.lock.json` and copies the `$ref`
closure of the files here (DEC-0017; `service-usage.schema.json` is the usage report's, DEC-0021, and
`model-profile.schema.json` and `registry-models.schema.json` are what the well-known document refers to at the pin). They are
normative for the descriptor, the well-known document, the events page and the usage report;
`tests/test_fabric_service.py` validates what the always-on server produces against them and checks the digests below.

Do not edit them here. When the pin moves, run the tool again: it rewrites the files, the commit above and the digests
below, and `tests/test_contract_lock.py` holds the commit equal to the pin.

| File | sha256 |
|---|---|
| `common.schema.json` | `707a28c49e8a370d3ace2898406093ad9bac93952054f05bbffeb0b912dd8e99` |
| `model-profile.schema.json` | `a08094f299904445056873c23e780c9c6a3def109a9c3c5c1c03ccf118227b5a` |
| `registry-models.schema.json` | `c0bbd35daa98dee966dc1f9f12917213fb8259ac301648973fdf6dd91b118561` |
| `service-common.schema.json` | `9f7d1d81f28b739b55a6cfc3526cd9c521a9aafdd17622975c0a30b1cf720c7b` |
| `service-descriptor.schema.json` | `474e18178f4a71be189ffe1028996efd0cccc060a7b929e2e969068a5de6b3c7` |
| `service-events-page.schema.json` | `d4e13e2147b983a0a3ecdc671fe9505418e1d848d7874695acf97955035d93cc` |
| `service-usage.schema.json` | `25cf7f07a0ab1d2407925f3ff8acdc9f5339456e5baad8118d3b79c3f6af8e71` |
| `service-well-known.schema.json` | `3b838b49d48f4facff5ad63308875425bc712892d124d1d3f45632b90f90dd24` |
