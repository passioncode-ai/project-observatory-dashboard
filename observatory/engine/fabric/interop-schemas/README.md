# fabric-interop/0.1 schemas (vendored)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit
`9cd778eb6f14b977f9a5b4930826f62dc95c5619` (`schemas/`), the commit of DEC-0017, the operator's rulings on the interop open
questions. They are normative for a job's answer (`interop-job.schema.json`), the handle, the
result envelope (`interop-result.schema.json` over `result.schema.json`) and a request's
trace context. `tests/test_interop.py` and `tools/run_probes.py` validate what the MCP server
answers against them; the server itself serves self-contained equivalents, since an MCP client
does not fetch a `$ref` (`mcp/capability_tools.py`).

Do not edit them here. When the contract changes, copy the files again from the new commit and
update the commit and the digests below (`test_interop.py` checks them).

| File | sha256 |
|---|---|
| `common.schema.json` | `b0b4f7dafa9ccffd345ec319c97cacf65b1983c2da874c63606ca60cab448fd5` |
| `interop-common.schema.json` | `ae795534c91966c8ef89a604c872a499986f17bc93dd14e8ce803c17217b4b74` |
| `interop-job-handle.schema.json` | `9b8b20bc978c0be4b75b42dd98af7b82586b6642983117248a1d5b2041c53645` |
| `interop-job-request.schema.json` | `36f649b0e84b13835bd328181c006466da08037be4e5190485b27e93644f4531` |
| `interop-job-tool-output.schema.json` | `d8c5ddbd9a331d6529cc7c62e643359cd685f0d3745262190311e4d50e0539f1` |
| `interop-job.schema.json` | `f45a612687544237e70c786f5363c711723314c592cd163535350cdc5cf5daa5` |
| `interop-request-meta.schema.json` | `04c1cab07c11a03d994a2bcbffb69e388822185decdc92a103c831610e7ff58c` |
| `interop-result.schema.json` | `8e17dbc0662b95c69ff8efeb2039518afd96a44185207a63e922b3e2893df322` |
| `result.schema.json` | `adb36e58c4628ec785d9e5dfd931fe425f69e87535b04e49c395655a47493fea` |
