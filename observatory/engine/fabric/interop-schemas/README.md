# fabric-interop/0.1 schemas (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit `f33f4d8ad3a15e3f4ea06e51cff00c64f4af4f6e` (the contract pin, `schemas/`)
by `tools/vendor_contract_schemas.py`, which takes the commit from `fabric-contract.lock.json` and copies the `$ref`
closure of the files here. They were first copied at the commit of DEC-0017, the operator's rulings on the interop open
questions. They are normative for a job's answer (`interop-job.schema.json`), the handle, the
result envelope (`interop-result.schema.json` over `result.schema.json`) and a request's
trace context. `tests/test_interop.py` and `tools/run_probes.py` validate what the MCP server
answers against them; the server itself serves self-contained equivalents, since an MCP client
does not fetch a `$ref` (`mcp/capability_tools.py`).

Do not edit them here. When the pin moves, run the tool again: it rewrites the files, the commit above and the digests
below (`test_interop.py` checks the digests, `tests/test_contract_lock.py` holds the commit equal to the pin).

| File | sha256 |
|---|---|
| `common.schema.json` | `707a28c49e8a370d3ace2898406093ad9bac93952054f05bbffeb0b912dd8e99` |
| `interop-common.schema.json` | `6ad2272474108d353625b702d63ffdc54710d7525d81eaa1960bbf338b17758a` |
| `interop-job-handle.schema.json` | `9b8b20bc978c0be4b75b42dd98af7b82586b6642983117248a1d5b2041c53645` |
| `interop-job-request.schema.json` | `36f649b0e84b13835bd328181c006466da08037be4e5190485b27e93644f4531` |
| `interop-job-tool-output.schema.json` | `09d83297be58e2ffd0a934b327398f4a68e6ca05f78a21cd1b50925c3ef7fd67` |
| `interop-job.schema.json` | `f45a612687544237e70c786f5363c711723314c592cd163535350cdc5cf5daa5` |
| `interop-request-meta.schema.json` | `ba3ce2692c4c08b99224b5e775b616f7370fe8f4296c166b366f6cba8937be46` |
| `interop-result.schema.json` | `8e17dbc0662b95c69ff8efeb2039518afd96a44185207a63e922b3e2893df322` |
| `result.schema.json` | `adb36e58c4628ec785d9e5dfd931fe425f69e87535b04e49c395655a47493fea` |
