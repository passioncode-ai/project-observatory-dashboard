# 2026-09-30 — `machine.mcp.inventory` and `fabric-interop/0.1` (0.9.0, 0.9.1)

Handoff for the next agent. No private names or ids here. Plan rows: AR-2.6 (Observatory side)
and AR-11.1 (Project Observatory) of Fabric's agent-registry plan; contract: fabric-agent-contract
`9cd778eb6f14` (interop.md, DEC-0016 and the rulings DEC-0017); adapter: fabric-agent-adapter
v0.5.0 (`f31c2b2792f7`). Design and rule-by-rule proof: [FABRIC-INTEROP.md](../../design/FABRIC-INTEROP.md)
and [FABRIC-CONFORMANCE.md](../../../observatory/engine/fabric/FABRIC-CONFORMANCE.md).

## What shipped

| PR | Merge commit | What |
|---|---|---|
| [#84](https://github.com/passioncode-ai/project-observatory-dashboard/pull/84) | `8f324ca` | `machine.mcp.inventory`: six agent-config formats, redaction at the reader (URL user-info now dropped too), every source read/absent/unreadable, stale and never-scanned answers; manifest revision 6; one schema-release pin per file; README quick start registers the MCP server |
| [#85](https://github.com/passioncode-ai/project-observatory-dashboard/pull/85) | `3b7e064` | `fabric-interop/0.1`: capabilities as tools of their names with the published schemas, trace context, `machine.mcp.refresh` as a job with `fabric.job.get`/`cancel`, full result envelope with `trace`; per-install manifest behind the descriptor; manifest revision 7; vendored contract schemas and adapter 0.5.0 kit/probe |
| [#86](https://github.com/passioncode-ai/project-observatory-dashboard/pull/86) | `fa46bad` | release 0.9.0 |
| [#87](https://github.com/passioncode-ai/project-observatory-dashboard/pull/87) | `c0c9fdb` | release 0.9.1: object-rooted output schemas (Claude Code refused 0.9.0's tool list) |

| Release | Tag commit | Wheel sha256 (GitHub digest and `SHA256SUMS` agree, re-downloaded) |
|---|---|---|
| [v0.9.0](https://github.com/passioncode-ai/project-observatory-dashboard/releases/tag/v0.9.0) | `fa46badee8ea` | `29d83e04ecb692b37c58f6dde97661df8a23514aa49993f43d3e013434bb5ffb` |
| [v0.9.1](https://github.com/passioncode-ai/project-observatory-dashboard/releases/tag/v0.9.1) | `c0c9fdbe044a` | `5c0b2ef4d9e0d90f6cc9ec2ea4690f017623f413c57252b66a973b6bbe3171ff` |

`tools/publish_contract.py --check` after v0.9.0: `publication_identical: true` — the v0.2.0
identifiers and the new v0.9.0 schemas resolve and match the bundle.

## Checks actually run

- All engine suites (`tests/run_portable.py`): main before, 194 suites / 5609 PASS assertions /
  488 cases; after #84, 195 / 5643 / 506; after #85 and at 0.9.1, **196 / 5662 / 521**, PASS.
- CI (three required rows) green on every PR; #85 needed two fixes found by CI, see below.
- Root `unittest`, `compileall` on 3.11, wheel + `check_package.py`, `update_inventory.py --check`,
  `check_public_release.py --history`: 0 on a clean clone of each branch.
- fabric-agent-adapter v0.5.0 `check_service.py` against a live server in a temporary workspace:
  26 rules, 0 FAIL, 7 NOT_RUN; against the installed launchd instance after the update: 27 rules,
  0 FAIL, 6 NOT_RUN. NOT_RUN: `login.single-use` (no login), the five `interop.*` rules that call
  MCP over HTTP (this server is stdio; covered by `tests/test_interop.py`), and `lifecycle.launchd`
  on the unmanaged test descriptor.
- A newcomer's path, from a clone of `v0.9.1` under a throwaway `HOME`, every step exit 0: README
  quick start (venv, install, `full init`, `configure sources projects`, `full local`,
  `full doctor`), `claude mcp add observatory --scope user -e OBSERVATORY_HOME=… -- <python>
  <engine>/mcp/server.py`, `claude mcp list` → `✔ Connected`, `configure sources
  mcp_config_root`, `configure integrations mcp true`, `full scan-mcp`, then `machine.mcp.inventory`
  through the registered command.

Planted defects, each watched failing and restored: URL user-info kept; an absent config skipped
in silence; `target` passed through the inventory; staleness never flagged; absent sources dropped
from `degraded`; the caller's span echoed instead of a child; a re-derived input schema; an unknown
job id answered as a fresh job; a cancel that leaves the work running; the job's trace not handed
to its child process; a dead runner reading as working; the envelope's trace disagreeing with
`_meta` (FAC-SEM-022); an envelope without `artifacts`; a job-tool union that is not the contract's;
a stale per-install manifest not refreshed on start; the root `"type": "object"` removed.

## What went wrong, and what now catches it

1. **0.9.0 made every tool unreachable from Claude Code.** Claude Code 2.1.285 refuses the whole
   `tools/list` answer when one `outputSchema` is not object-rooted; two were `oneOf`-rooted. The
   Python SDK client the suites use accepts them. Found by the newcomer check, fixed in 0.9.1;
   `test_every_mcp_capability_is_a_tool_with_its_published_schemas` now holds every tool to
   object-rooted schemas. No suite drives the real Claude Code client (next task 1).
2. **A cancel test passed on macOS and failed on Linux**: an unreaped runner is a zombie that
   Linux still counts in its process group. The test now counts live members only.
3. **The conformance suite crossed the runner's 120 s budget on one CI row** (116 s, then a
   TIMEOUT): every read capability started its own MCP server per probe run. They share one
   session now (55–71 s on CI).
4. **A failing assertion hung `test_interop.py` until the timeout**: its session helper entered
   the SDK's context managers by hand. They are nested now.
5. `publish_contract.py --local-manifest` named the base interpreter behind a virtual
   environment's `python`; `observatory_status` skipped the value check for a scope object. Both
   fixed with tests.

## Decisions

- The inventory answers from the tick's scan (`store/raw/mcp.json`); `machine.mcp.refresh` is the
  job that re-takes it. `agent` is the runner-catalogue kind; files are `~/`-relative; servers are
  grouped by name and transport; `needs-auth` counts as `answers: true`.
- An absent agent config is named in the capability's `degraded` but does not degrade the scan
  (and so not the service's well-known status).
- The envelope's `scope` carries the observatory's own URNs: the call carries none of the host's.
- New schemas are pinned to the release that publishes them; v0.2.0 identifiers are untouched.

## Open work

1. **Drive the real Claude Code client in CI**: `claude mcp list` against a synthetic workspace on
   the row that installs Claude Code, failing on anything but `✔ Connected`.
2. For the contract (not this repository): the DEC-0017 job-tool union needs `"type": "object"`
   at its root for MCP clients; the envelope's `scope` requires identifiers a callee never
   receives; `fabric-contract.lock.json` here predates the contract-pin schema (G-11) and has
   another shape.
3. Serve MCP over HTTP from the always-on server (`surfaces.mcp`), so the adapter's interop probe
   rules and C3.6 can run against it.
4. Liveness for agents other than Claude Code (no probe CLI) and project-level `.mcp.json` files
   are not inventoried.
