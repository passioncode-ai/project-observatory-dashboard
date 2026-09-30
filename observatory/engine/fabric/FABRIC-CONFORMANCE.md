# Local MCP profile and verification limits

Project Observatory publishes a self-contained local MCP profile at
https://github.com/ssheleg/project-observatory-open-source. The provider manifest
is revision 7. `fabric-contract.lock.json` selects profile `observatory-local-mcp`
version `1.2.0`; `fabric-agent.json` retains contract version `0.1.0`. These are
separate version axes.

**Two schema releases, one pin per file.** The lock's `releases` lists, for every
bundled schema and fixture, the one release tag that published it: the first eight
schemas and six fixtures at `v0.2.0`, under the repository's address at the time
(`ssheleg/project-observatory-open-source`, which GitHub redirects), and the
`machine.mcp.inventory` and `machine.mcp.refresh` schemas and fixtures at `v0.9.0`, under
`passioncode-ai/project-observatory-dashboard`. Published identifiers are never
rewritten, so a capability added later is pinned to the release that adds it
instead of moving the old ones. `tools/publish_contract.py --local` refuses a file
listed under no release or under two, a URI no release publishes, and a schema
whose `$id` is not its own pinned address.

This profile preserves existing MCP tool names, input aliases and JSON schema
field shapes. External Fabric host admission is **unverified**. The repository
does not ship a private external contract or claim external certification.

## Declared surface

The capability definitions and effects come from `../fabric-agent.json`:

| Capability | Effect | Served as (fabric-interop/0.1) | Older tools required by the capability |
| --- | --- | --- | --- |
| `estate.survey` | `none` | `estate.survey` | `observatory_status` |
| `project.detail` | `none` | `project.detail` | `observatory_project` |
| `project.timeline` | `none` | `project.timeline` | `observatory_timeline` |
| `project.record` | `draft` | `project.record` | `observatory_record`, `observatory_propose`, `observatory_recall` |
| `machine.mcp.inventory` | `none` | `machine.mcp.inventory` | — |
| `machine.mcp.refresh` | `none`, job | `machine.mcp.refresh`, then `fabric.job.get` / `fabric.job.cancel` | — |

The ten `observatory_*` tools stay, unchanged, for hosts that call them; the full
server also exposes `observatory_credentials`, `observatory_search`,
`observatory_findings` and `observatory_machine`. With the six capability tools and
the two job tools that is eighteen tools, all defined through `../mcp/server.py`.

## fabric-interop/0.1

The Fabric agent-registry contracts (locked 2026-09-29, section C3) define how agents
are called through Fabric's hub. The manifest declares the extension
(`provider.extensions["https://fabric.passioncode.ai/agent-contract/extensions/interop/0.1"] = {}`),
and the server keeps its rules; each is proved in `../tests/test_interop.py` (14 cases)
and `../tests/test_mcp_wire.py`, and the reading behind each choice is in the
repository's [interop design](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/FABRIC-INTEROP.md):

- **C3.1** every `mcp` capability is a tool of the same name whose `inputSchema` and
  `outputSchema` are the bundled published files, compared byte for byte over the wire
  (FAC-SEM-017). Arguments are validated against the input schema and answers against
  the output schema; an answer the schema does not describe leaves as `isError`.
  Annotations: `effect: none` → `readOnlyHint`; a non-destructive write says
  `destructiveHint: false` explicitly, since MCP defaults it to true.
- **C3.2** `machine.mcp.refresh` declares `"job": true` and answers
  `{"job": {"id", "status": "working"}}`; `fabric.job.get` and `fabric.job.cancel`
  answer the contract's `interop-job.schema.json`. Its tool's `outputSchema` is the
  DEC-0017 union `oneOf[result envelope, job handle]` around the capability's output
  schema; the result is the full result envelope with `output`, `usage` and `trace`.
  MCP Tasks are not offered.
- **C3.4** a request's `_meta.traceparent` becomes a child span; every answer carries
  its span back in `_meta.traceparent` (and `tracestate`); answers about a job carry the
  job's, equal to the envelope's `trace` (FAC-SEM-022); a job hands a child span to the
  process it runs as `TRACEPARENT`. No event on the feed is about traced work, so none
  carries a trace pair (DEC-0017, OQ-0007).

The contract schemas these answers are validated against are vendored in
`interop-schemas/` from fabric-agent-contract commit `9cd778eb6f14` (DEC-0017).

## Per-install manifest

`tools/serverd.py --install` writes `$OBSERVATORY_HOME/config/fabric-agent.json`
(mode 600) before the service descriptor, and the descriptor's `fabricManifest` points
at it: the same manifest with this installation's interpreter and `mcp/server.py` as
the stdio connection, the workspace as `--home` (a stdio connection carries arguments,
not an environment) and the service extension naming this workspace's own descriptor.
The portable manifest stays a template.

## `machine.mcp.inventory` and contract C6

The Fabric agent-registry contracts (locked 2026-09-29, section C6) fix the answer as
`{servers: [{name, declaredIn: [{agent, file}], transport, answers, checkedAt}],
inventoryAt}`. The observatory serves exactly those fields and adds, all optional to a
reader of C6:

| Addition | Why |
|---|---|
| `degraded` | every observatory answer carries one: an absent or unreadable agent config, a probe that did not finish, and an inventory older than the tick's staleness bound are named there |
| `sources` | each agent config read, with `read`, `absent` or `unreadable`, so a host can say how complete the list is |
| `servers[].status`, `declaredIn[].status` | `connected`, `needs-auth`, `failed`, `not-listed`, `not-probed`, `disabled`: `answers` alone cannot tell "asked to sign in" from "up", or "switched off" from "never probed" |
| `declaredIn[].scope`, `declaredIn[].disabled` | a Claude Code project scope, plugin or claude.ai connector, and a server switched off in its own config |

Choices C6 leaves open, stated: `agent` is the runner-catalogue kind (`claude-code`,
`cursor-agent`, `opencode`, `codex`, `gemini-cli`, `kiro`); `file` is home-relative
(`~/.claude.json`) and null for a server a plugin or connector ships; servers are
grouped by name and transport, so one name declared with two transports is two rows;
`needs-auth` counts as `answers: true` (the server answered, asking for
authorization); `checkedAt` is the probe's time and null when nothing probed the
server; `transport` is null only when neither the declaration nor a probe line said.
Liveness comes from Claude Code's own probe, so a server only another agent declares
answers `null`. Project-scoped `.mcp.json` files inside repositories are not read.
Credential inventory returns names and metadata, not stored values. Recording
and proposal tools write private local memory; they do not authorize provider
changes or deployment.

**Three MCP resources, and they are protocol surface rather than contract
surface.** `observatory://estate` (JSON), `observatory://project/{project_id}`
(JSON, one URI per project — the address a renderer holds) and
`observatory://dashboard` (`text/html`) are served by `../mcp/server.py`. The
pinned contract defines no resource concept and no rendering capability, so the
manifest declares neither; a host that relies on these resources relies on MCP,
not on the contract.

Schemas live in `schemas/`, with fictional requests in `fixtures/`. Published
schema URLs are pinned to the release selected by the lock. An application patch
release does not silently move that schema pin.

## Local service extension

Revision 5 adds one key under `provider.extensions`:
`https://fabric.passioncode.ai/agent-contract/extensions/service/0.1` with the value
`{"descriptor": "project-observatory.default"}`. It names the `fabric-service/0.1` descriptor
that `tools/serverd.py --install` writes for the standard workspace; another workspace's
descriptor carries its own instance, `ws-<sha16>`. Discovery grants nothing: the descriptor makes
the always-on server visible to a local host and does not replace admission or binding. The
server's well-known document, events feed, lock and installer are described, with their tests,
in the repository's [service design](https://github.com/passioncode-ai/project-observatory-dashboard/blob/main/docs/design/FABRIC-SERVICE.md); the bundled service schemas in
`service-schemas/` are copied from the contract and used by those tests.

## Checks that can be reproduced

From the engine directory, run:

```sh
python tools/publish_contract.py --local
python tools/fabric_hash.py --check
```

The first command checks bundled schema/fixture references, the portable
connection template and manifest hash. The second independently recomputes the
hash. The algorithm hashes the entire manifest with `provider.contentHash`
omitted: UTF-8 JSON, sorted keys, compact separators and no ASCII escaping,
then SHA-256 prefixed with `sha256:`. It does not cover remote file contents.

After the pinned release is publicly available, run:

```sh
python tools/publish_contract.py --check
```

That command fetches every pinned schema and fixture anonymously and compares
its contents with this checkout. Local validation is not evidence that these
public URLs resolve. A failed remote check blocks a claim of published-schema
availability.

Runtime probes require an explicitly synthetic, initialized workspace whose
settings enable `features.probe_fixture` and contain no configured integrations
or external sources. Populate it with fictional data matching the selected
fixtures, then run:

```sh
python tools/run_probes.py --fixture-home /absolute/path/to/synthetic-workspace
python tools/run_probes.py --fixture-home /absolute/path/to/synthetic-workspace --check
```

The first run stores receipts at `store/probe-receipts.json` inside that private
workspace. The check run repeats probes without rewriting those receipts. The
probe implementation in `../tools/run_probes.py` checks output schema and
semantic assertions, refused identity inference, store-degradation behavior,
and read-only capability side effects. Draft-effect probes can write to the
synthetic workspace. Never point them at a working installation. No historical
operator receipts or measured project counts are included in this distribution.

## Per-install connection

The public manifest uses `observatory-install:mcp-server` as a template reference,
not an executable path on somebody else's computer. After initializing a private
workspace, generate a local manifest at a new destination beneath it:

```sh
python tools/publish_contract.py --local-manifest "$OBSERVATORY_HOME/config/mcp-profile.json"
```

The generated file resolves the current interpreter and installed server path,
recomputes its own manifest hash and uses private file permissions. The generator
refuses symlinks and existing destinations. This per-install manifest contains
machine paths and belongs in the user's private workspace, never in the public
repository. Configure the chosen MCP host with the installed server command;
the manifest alone does not register it with an external host.

## Compatibility limits

`asOfScanId` is a comparison request, not historical snapshot retrieval. Registry
answers use the current data and report an unavailable requested scan through
`degraded`. Cursor pagination is ordered by project id; intervening registry
changes can affect a multi-page walk. Optional absent measurements do not mean
zero activity. These limits are encoded in the corresponding schema descriptions
and checked by local probes where applicable; they are not external host
conformance guarantees.
