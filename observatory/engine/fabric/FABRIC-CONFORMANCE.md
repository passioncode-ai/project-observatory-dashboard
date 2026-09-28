# Local MCP profile and verification limits

Project Observatory publishes a self-contained local MCP profile at
https://github.com/ssheleg/project-observatory-open-source. The provider manifest
is revision 5. `fabric-contract.lock.json` selects profile `observatory-local-mcp`
version `1.0.0` and schema release `v0.2.0`; `fabric-agent.json` retains contract
version `0.1.0`. These are separate version axes.

This profile preserves existing MCP tool names, input aliases and JSON schema
field shapes. External Fabric host admission is **unverified**. The repository
does not ship a private external contract or claim external certification.

## Declared surface

The capability definitions and effects come from `../fabric-agent.json`:

| Capability | Effect | MCP tools required by the capability |
| --- | --- | --- |
| `estate.survey` | `none` | `observatory_status` |
| `project.detail` | `none` | `observatory_project` |
| `project.timeline` | `none` | `observatory_timeline` |
| `project.record` | `draft` | `observatory_record`, `observatory_propose`, `observatory_recall` |

The full server also exposes `observatory_credentials`, `observatory_search`,
`observatory_findings` and `observatory_machine`: ten tools in total, defined in
`../mcp/server.py`.
Credential inventory returns names and metadata, not stored values. Recording
and proposal tools write private local memory; they do not authorize provider
changes or deployment.

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
