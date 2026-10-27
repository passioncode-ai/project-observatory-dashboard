# fabric-interop/0.1 schemas (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit `759704b443078967c32ea3bb43588ea5f45cb9cd` (the contract pin, `schemas/`)
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
| `decision.schema.json` | `d20676bce5586cd992dfc9950a4055384c8bed9a86eb8a002ab90ad5573e2a42` |
| `interop-common.schema.json` | `8e70664fcdc0c4da610a53bfd27dc072a72e0cfd92095be39d41a373c73bf9f7` |
| `interop-job-handle.schema.json` | `9b8b20bc978c0be4b75b42dd98af7b82586b6642983117248a1d5b2041c53645` |
| `interop-job-request.schema.json` | `36f649b0e84b13835bd328181c006466da08037be4e5190485b27e93644f4531` |
| `interop-job-tool-output.schema.json` | `09d83297be58e2ffd0a934b327398f4a68e6ca05f78a21cd1b50925c3ef7fd67` |
| `interop-job.schema.json` | `ec8233058a83924abc5c876a6cd23b17460d27a66bf5ba83a5a2e3ad76790052` |
| `interop-request-meta.schema.json` | `fe6d79de680266d78a47cb1ebc9d6e2ac47713fba3bd98bb318db0b7ff097bc5` |
| `interop-result.schema.json` | `8e17dbc0662b95c69ff8efeb2039518afd96a44185207a63e922b3e2893df322` |
| `mandate.schema.json` | `48013956cb6da53f32928af0202a4f211702b47685287702ae1a7d30e3fcfb64` |
| `result.schema.json` | `adb36e58c4628ec785d9e5dfd931fe425f69e87535b04e49c395655a47493fea` |
