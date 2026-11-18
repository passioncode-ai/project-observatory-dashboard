# A project's stack from its dependency inventory, and its packages on the panel (OBS-603, CP-06)

Status: built, 2026-11-18. Requirement CP-06 of [CONTROL-PLANE.md](../CONTROL-PLANE.md) ("a project's stack follows
from it"); the inventory itself is M4, [dependencies.md](dependencies.md).

## What

Before this row a project's `stack` was a guess from marker files at the top of each folder
(`collectors/scan_filesystem.MARKERS`): a `package.json` said `node`, a `next.config.js` said `nextjs`. The dependency
scan already reads every lockfile a project resolves, at any depth its walk allows, so the part of the stack a package
manager knows now follows from that inventory, and the project panel lists the packages.

| Piece | File |
|---|---|
| Derivation, panel rows, survey and detail fields | `observatory/engine/project_stack.py` |
| Registry fields, at every emit | `observatory/engine/collectors/emit_registry.py` (reads `store/raw/dependencies.json`) |
| `project.detail` fields, the survey's degraded line | `observatory/engine/survey.py` |
| Panel section | `observatory/engine/dashboard/build_dashboard.py` (`packagesBlock`, `stackSourceLine`), payload `packages` (projects page only, `dashboard/shell.slice_for`) |

## Data

**Derivation** (`project_stack.derive(markers, inventory)`):

- An ecosystem with at least one lockfile READ (a file that failed to parse is not evidence) gives its kind: npm
  `node`, PyPI `python`, Go `go`, crates.io `rust`, RubyGems `ruby`, Packagist `php`.
- A DIRECT dependency that is a framework gives its kind: `next` `nextjs`, `astro` `astro`. A transitive one does not.
- A marker for a kind a lockfile could show (the six languages, the two frameworks) stands in only when no lockfile
  shows it, and is named in `degraded`: `marker-fallback` when other lockfiles were read, `markers-only` when the
  project has no readable lockfile at all.
- A marker for a kind no lockfile describes (`docker`, `ios`, `swift`, `android`, `flutter`, `agent-skill`,
  `claude-plugin`, `vercel`, `netlify`, `cloudflare-worker`) is kept and labelled `from: marker`; nothing is guessed
  about it, so it is not degraded.
- A stack set by hand in `config/project_overrides.json` wins and reads `stack_source: curated`.

**Registry** (`registry/projects.json`, per project): `stack` (the same field, now derived), `stack_source`
(`dependencies`, `dependencies+markers`, `markers`, `curated` or `none`), `stack_evidence`
(`[{kind, from, ecosystem?, package?}]`) and `stack_degraded` (`[{source, code, reason}]`, absent when empty). A stack
read from lockfiles cites SRC-0024, the dependency scan.

**Survey and detail.** `estate.survey` keeps its published fields (its item schema is closed): `stack` is the derived
list, and `degraded` gains one `{source: "stack", reason}` line counting the projects whose stack came from markers
(for a single-project scope, that project's own reason). `project.detail` (open project schema) adds `stackSource` and
`stackEvidence`.

**Panel** (`project_stack.panel(registry dependencies, vulnerabilities)`): per project at most 200 rows `{name,
version, ecosystem, direct, local, advisories}`, the vulnerable first (most advisories first), then direct, then by
ecosystem and name; counts `total`, `shown`, `direct`, `transitive`, `unknown`, `local`, `vulnerable`. `advisories` is
`null` when the watch has not run or is switched off: unknown, never 0.

## Wireframe (project panel, under "Technology and documentation")

```
Technology and documentation
[nextjs] [node] [python] . 2 notes
Read from the project's lockfiles.
Packages
Direct 12 . transitive 340 . local 1 . with advisories 1
Package   | Version | Ecosystem | Dependency            | Advisories
lodash    | 4.17.20 | npm       | direct dependency     | (2)
own-lib   | 0.0.0   | npm       | local dependency      | 0
...
200 of 353 shown, the vulnerable first; an agent reads them all with observatory_dependencies.
```

No package: "No lockfile or manifest of this project was read, so its packages are not known." A source that could
not be read adds "some sources could not be read" to the count line.

## Contract

- No new MCP tool: `observatory_dependencies(projectId)` already pages every package with its advisories, and
  `observatory_project` carries `stackSource` and `stackEvidence`. The panel has no button (parity unchanged).
- Strings: English ids in the page script, Russian in `dashboard/locales/ru.json`.

## Failure behaviour

- No dependency scan yet, or an unreadable one: every project's stack is its markers, `stack_source: markers`, each
  named in `stack_degraded`; the panel says the packages are not known.
- A lockfile that does not parse is not evidence for its ecosystem; its marker stands in and is named.
- The vulnerability watch not run or off: advisory cells read "not checked", and the panel's `degraded` says why.

## Tests

`observatory/engine/tests/test_project_stack.py` (suite `project_stack`): ecosystems and direct frameworks from
lockfiles, a transitive framework ignored, platform markers kept and labelled, marker fallback and markers-only named,
an unparsed lockfile not counted, a read lockfile without pins still naming its ecosystem, the registry fields, the
survey keeping its published fields while the detail adds the source, the survey's degraded line per project and per
estate, the panel rows, order, counts, cut and unknown advisory counts, and the emitter writing the derived stack over
a synthetic workspace (`emitter_fixture`). `test_project_surface` validates the live survey against its closed schema.
