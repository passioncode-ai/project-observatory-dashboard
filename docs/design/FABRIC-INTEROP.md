# The MCP server speaks fabric-interop/0.1

Status: implemented on branch `agent/fabric-interop`. The protocol is `fabric-interop/0.1`, section
C3 of the Fabric agent-registry contracts (locked 2026-09-29), whose normative home is the Fabric
Agent Contract's `docs/specification/interop.md` at commit `9cd778eb6f14` — DEC-0016 with the
operator's rulings DEC-0017 (OQ-0001…0007). The contract schemas this server is held to are
vendored, byte for byte, in `observatory/engine/fabric/interop-schemas/` (digests in its README,
checked by a test). Every rule names the test that proves it:
`observatory/engine/tests/test_interop.py` (suite `interop`) and
`observatory/engine/tests/test_mcp_wire.py` (suite `mcp_wire`).

## What it is

Fabric calls one agent from another through its own MCP hub. For the observatory, as a provider,
that means: every capability of `fabric-agent.json` is an MCP tool under the capability's own
name, taking and returning exactly its published schemas; work that outlives a request is a job
behind a handle; and the W3C trace context a request carries continues through the work it
starts. The ten `observatory_*` tools are unchanged; the capability tools answer from the same
code (`@server.capability` handlers in `mcp/server.py`).

| Module | Holds |
|---|---|
| `interop.py` | trace context (`Span`, `span_for`), tool definitions from the manifest, annotations, the result envelope |
| `jobs.py` | job records under `store/jobs/`, locking, reconciliation of dead runners, cancel, join, retention |
| `tools/run_job.py` | the detached runner: heartbeat, the work, the envelope; SIGTERM ends it |
| `mcp/capability_tools.py` | `InteropServer`: listing and answering the capability and job tools |

## Rules and their proof

1. **A capability is a tool of its name with its published schemas** (C3.1, FAC-SEM-017). The
   listing is built from the manifest and the bundled schema files, never from a Python
   signature. A job capability's tool serves, as DEC-0017 (OQ-0006) rules, the self-contained
   union `oneOf[result envelope, job handle]` around the capability's output schema — exactly
   the contract's `jobToolOutputSchema(output)` — so its structured content always conforms.
   - `test_every_mcp_capability_is_a_tool_with_its_published_schemas` compares the schemas a
     client receives with the bundled files, and the annotations with the declared effect;
     `test_the_served_instructions_describe_the_served_surface` (suite `wire_contract`) requires a
     handler for every capability that is not a job, and `mcp/server.py` refuses to start without
     one.
2. **The published schemas are enforced both ways.** Arguments that break the input schema are an
   `invalid-input` error answer naming the path and the rule, never the caller's value. An answer
   the output schema does not describe — a refusal the v0.2.0 record schema has no word for, such
   as `owner refused` — leaves as `isError` with its JSON as text, so a validating client never
   receives structured content it must reject. A proposal's `warning` has no field in that schema
   and travels as a sentence in the text channel.
   - `test_input_that_breaks_the_published_schema_is_an_error_answer`; in `mcp_wire`: the
     snake-case alias refused under the capability name, a missing owner refused, the operator's
     authority refused as an error answer, the warning kept in the text channel.
3. **The trace continues** (C3.4 a, b). `_meta.traceparent` (lower-case W3C, version 00) becomes
   the parent of a new span with the same trace id; the answer's `_meta.traceparent` is that
   span, and `tracestate` is passed back unchanged. A missing, upper-case, all-zero or version-ff
   traceparent starts a new trace rather than an invented parent. A job's answers carry the job's
   span, and the process a job runs receives a child span as `TRACEPARENT`/`TRACESTATE`.
   - `test_the_answer_is_a_child_span_of_the_callers`, `test_no_or_an_invalid_traceparent_starts_a_new_trace`,
     `test_a_job_runs_to_a_result_envelope_on_the_callers_trace` (the stand-in `claude` writes the
     trace it was started with).
4. **Jobs** (C3.2). `machine.mcp.refresh` answers `{"job": {"id", "status": "working"}}`;
   `fabric.job.get` follows it to `completed`, `fabric.job.cancel` stops it. Every answer is
   validated against the contract's `interop-job.schema.json`: `result` exactly when completed,
   `error` (`{code, message}`) exactly when failed. The result is the FULL result envelope
   (DEC-0017, OQ-0002): `id`, `contractVersion`, `outcome`, `done`, `proof`, `scope`,
   `notVerified`, `artifacts`, `createdAt`, `producer`, `output`, `usage` — and `trace`, the job's
   span, which is authoritative (OQ-0003); every answer about the job carries the same
   traceparent in `_meta`, as FAC-SEM-022 requires.
   - an id never issued, or malformed, is `unknown-job` and creates nothing:
     `test_an_id_never_issued_is_unknown_and_never_a_fresh_job`;
   - a job outlives the server that started it — its record is a file and its work a detached
     process: `test_a_job_outlives_the_server_that_started_it`;
   - a cancel stops the runner's whole process group and is not overwritten by its exit:
     `test_a_cancel_stops_the_work_and_stays_cancelled`;
   - a runner killed mid-work reads as `failed` / `interrupted`, never `working` for ever:
     `test_a_runner_killed_mid_work_reads_as_interrupted`;
   - a second start while one runs joins it: `test_a_second_start_joins_the_running_job`.
5. **Nothing planted in an agent config reaches an answer**, through the job and the inventory:
   `test_the_inventory_answer_validates_and_holds_no_planted_value`.
6. **The descriptor points at a manifest a host can run.** `tools/serverd.py --install` writes
   `config/fabric-agent.json` first: this installation's interpreter (the virtual environment's,
   not the resolved base interpreter, which lacks the packages), `mcp/server.py`, `--home` with
   the workspace, and the service extension naming this workspace's descriptor.
   An update restarts the server without re-running the installer, so the server rewrites that
   manifest when it starts, after its lock, if the installer wrote one and it no longer matches
   the running code.
   - `test_install_writes_the_descriptor_then_the_plist` (suite `fabric_service`),
     `test_the_per_install_manifest_starts_a_server_for_its_own_workspace`,
     `test_a_start_refreshes_a_stale_per_install_manifest_and_creates_none`.
7. **The published probes run the job path.** `tools/run_probes.py` probes
   `mcp-refresh-runs-as-a-job` end to end, bounded by its `timeoutMs`; the conformance suite
   (`conformance_receipt`) runs every declared probe.

## Limits, stated

- **Two served output schemas carry one keyword the published bytes do not.** MCP clients require
  an object schema at the root of a tool's `outputSchema`; Claude Code 2.1.285 refuses the whole
  tool list otherwise ("Handler returned an invalid result"), which made every tool unreachable
  in 0.9.0. `project.record`'s v0.2.0 output schema and the DEC-0017 job-tool union are both
  rooted in `oneOf`, so they are served with `"type": "object"` added at the root
  (`interop.object_rooted`); every branch is an object, so the accepted values are unchanged. A
  strict byte comparison under FAC-SEM-017 sees the added keyword; the contract's union needs
  the same root to be usable by that client.

- **C3.4 (c), trace ids on events: nothing to carry.** DEC-0017 (OQ-0007) rules that only an
  event about traced work carries `traceId`/`spanId`, and one about untraced work must not
  invent them. The feed is a view over commits, agent sessions and findings, none of which a
  traced request causes, so no event carries a pair. A job does not publish events.
- **C3.6, `surfaces.mcp.capabilities` in the well-known document, is not served**, and neither
  are the probe's MCP rules over HTTP: this MCP server is stdio, the always-on HTTP server has no
  `/mcp` surface, so `check_service.py`'s `interop.tools-match`, `interop.job-tools`,
  `interop.unknown-job` and `interop.trace-propagation` report NOT_RUN ("no MCP surface"). The
  rules themselves are checked over stdio by `test_interop.py`.
- **`scope` in the envelope names the observatory's own identifiers.** The contract requires the
  project, run, node and binding, which are the host's and which a tool call does not carry; the
  envelope uses `urn:observatory:subject:machine`, `urn:observatory:job:<id>`,
  `urn:observatory:capability:<name>` and this provider's revision, so a host cannot mistake them
  for its own.
- **MCP Tasks are not offered.** The SDK in use (`mcp` 2.2.0) implements no Tasks extension, and the
  contract makes the job handle the default.
- **No capability elicits input**, so C3.3 and FAC-SEM-018 have nothing to apply to; `input_required`
  is a known state that no job enters.
- **The envelope's `scope` carries `writeScopes` only.** The contract's result envelope also names
  the host's project, run, node and binding, which a tool call does not give the provider.
- **Usage is zero tokens.** No model runs in these jobs; `wallMs` is measured.
- **The adapter's probe and this server.** fabric-agent-adapter v0.5.0's `check_service.py` adds
  seven `interop.*` rules; against this server `interop.manifest-link` (the per-install manifest
  names this descriptor) and `interop.events-trace` apply, and the four that call MCP over HTTP
  report NOT_RUN, as above. v0.5.0 predates DEC-0017 (its kit still serves a job tool's plain
  output schema); this server follows the contract, not the kit.
