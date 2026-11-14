# M4: dependency inventory and vulnerability watch (OBS-553)

Status: built, 2026-11-14. Requirements CP-06 and CP-07, decision D2 and the contracts `registry/dependencies.json`
and `dep.vulnerable` of [CONTROL-PLANE.md](../CONTROL-PLANE.md). Public sources: [research.md](research.md),
"Vulnerability data" and "Lockfiles readable with the standard library".

## What

| Piece | File | Runs |
|---|---|---|
| Lockfile readers | `observatory/engine/collectors/scan_dependencies.py` | every tick (`scan-dependencies`), `full scan-dependencies` |
| Registry document | `observatory/engine/collectors/dependencies_registry.py`, called by `emit` | every emit |
| OSV and known-exploited watch | `observatory/engine/tools/vuln_watch.py` | every tick (`vulnerabilities`), asks only when due; `full vulnerabilities [--force]` |
| Findings | `observatory/engine/tools/dependency_findings.py`, called by `build_findings.py` | every findings build |
| Agent view | `vuln_watch.agent_view`, served as the MCP tool `observatory_dependencies` | on request |

## Data

**Readers** (standard library only). npm: `package-lock.json` and `npm-shrinkwrap.json` v2 and v3 (the `packages`
map; `link: true` skipped; a workspace member keyed by its folder is local), `pnpm-lock.yaml` v5, v6 and v9 and
`yarn.lock` v1 and Berry (narrow line readers of the subset each format uses), and `package.json` alone when no lock
is beside it (its ranges are kept with `pinned: false`). PyPI: `Pipfile.lock`, `poetry.lock`, `uv.lock`,
`pylock.toml`, `requirements*.txt` (`==` pins; a range is skipped). Go: `go.mod` `require` lines and blocks
(`// indirect` is transitive; a `replace` onto a path is local; the `v` prefix is dropped, as OSV spells Go
versions). crates.io: `Cargo.lock` (no `source` is a workspace member). RubyGems: `Gemfile.lock` (`GEM` specs;
`PATH` and `GIT` are local; a platform suffix is dropped). Packagist: `composer.lock` (a `path` dist is local; a
leading `v` is dropped, not verified for every package). Direct names come from the lock's own root
(npm, uv, Cargo, pnpm importers, Gemfile `DEPENDENCIES`, `go.mod`) or from the manifest beside it (`package.json`,
`pyproject.toml`, `Pipfile`, `composer.json`); `direct: null` where neither says.

**Local** means a path, a workspace member, a link, a git URL, a package resolved from a registry other than the
public one, or a name in `config/private_packages.json` (`{"names": [...], "npm_scopes": ["@example-org"]}`).
`source` names which.

**Walk.** Each project's `local_folders` from `registry/projects.json`, at most three folders down, never into
installed trees, build output or fixtures (`SKIP_DIRS`). A file whose size and modification time equal the previous
scan's keeps its parsed rows.

**Files.** `store/raw/dependencies.json` (`dependencies-scan/1`); `registry/dependencies.json` (contract shape:
per project `ecosystems`, `packages:[{name, version, ecosystem, direct, local, file}]`, `read_from`, `degraded`;
no scan time, so the registry changes only when its content does); `store/raw/vulnerabilities.json`
(`vulnerabilities/1`); `store/state/osv-cache/<id>.json` and `kev.json`; `vuln-watch.json` in the store folder.

## Contract

- **OSV.** `POST https://api.osv.dev/v1/querybatch` with up to 1,000 queries, one per public pinned package, deduped
  across projects; `next_page_token` is followed (at most 20 pages a query). Each advisory's record
  (`GET /v1/vulns/{id}`) is fetched only when it is not cached or OSV lists a newer `modified`.
- **Known exploited.** CISA's catalogue JSON, read at most once a UTC day, joined on an advisory's CVE aliases.
- **Due.** A pass runs when the last complete one was on another UTC day, or the inventory digest changed. Every
  pass asks about every package, so an advisory published after a lockfile was scanned is matched next day.
- **Severity.** The advisory database's word (`database_specific.severity`) first, then a CVSS v3 vector's base
  score (FIRST v3.1 formula; 9.0 critical, 7.0 high, 4.0 medium); a CVSS v4 vector alone reads `unknown`.
- **Finding** `dep.vulnerable`: one per project, package and version, all its advisories in it, because the remedy is
  one upgrade and an acknowledgement silences one decision (the derivation and SSH key rules use the same grain).
  Severity from the worst advisory: critical to critical; high or unknown to warning; medium or low to info; then
  one level higher when any advisory is exploited. It carries `advisory`, `advisories`, `aliases`, `fixed` (the
  lowest listed fix above the version by a plain numeric order, `null` when none), `exploited`, `lockfile` (relative
  to the project folder) and the subject `dep:<folder>/<ecosystem>/<name>@<version>`, which `finding_matcher`
  attributes to the project. `dep.watch_stale` (warning) when the last complete pass is older than three days.
- **Switch.** `integrations.vulnerabilities`, on when absent (`configuration.DEFAULT_ON`); `false` or
  `OBSERVATORY_OFFLINE=1` asks nothing and writes `enabled: false`, and the findings go quiet.
- **MCP.** `observatory_dependencies(projectId?, vulnerableOnly?, limit 1..500, cursor?)`: package rows, the
  vulnerable first, each with its advisories; `total`, `nextCursor`, `scannedOn`, `checkedOn`, `degraded`; refusals
  `unknown project` and `unknown cursor` are typed answers.

## Failure behaviour

- A lockfile that does not parse: its row carries `error`, the project's `degraded` names it ("unknown, not
  absent"), `totals.unreadable_files` counts it. A lockfile v1 is named the same way.
- The scan out of time: the projects not reached keep the previous scan's rows, `complete: false`.
- OSV unreachable or answering malformed JSON: the previous answer is kept with its own `checked_on`,
  `complete: false`, and `degraded` names the day; the state is not advanced, so the next tick asks again.
- An advisory record unreadable: the cached one is used if any, and `degraded` says so.
- The catalogue unreadable: the cached copy with its day, else no advisory is marked exploited, and `degraded` says so.

## Not done here

- osv-scanner is not run: it reads lockfiles itself and would send local and private names this module withholds;
  using it would need a filter over its own extraction first.
- The Fabric connections declaration (`service_connections.PROVIDERS`) does not list OSV and CISA yet: its staleness
  rule reads the tick's `stale()` gates, and this watch decides its own day.
- No dashboard section shows the inventory yet (M2 consumes the contract).
- Version ordering per ecosystem (npm semver, PEP 440, Ruby) is not applied to `fixed`; OSV's own matching decides
  which versions are affected.

## Tests

`observatory/engine/tests/test_dependencies.py` (suite `dependencies`): every reader on a written fixture; walk
skips; the cache by size and time; an unparseable file in `degraded`; the registry shape; local, private and
unpinned packages absent from every request a fake fetcher records; pagination; record reuse by `modified`; the
catalogue once a day and the exploited boost; offline keeps the last answer with its date; the switch; the due rule;
CVSS v3 scores; the finding fields and grouping; the agent view's paging and refusals. `test_mcp_wire` calls
`observatory_dependencies` over stdio before any scan.
