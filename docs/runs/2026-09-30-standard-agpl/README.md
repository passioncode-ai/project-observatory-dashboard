# 2026-09-30 — AGPL-3.0 or commercial, and the repository standard

Handoff for the next agent. No private names or ids here.

## Objective

Bring this repository onto the organisation's repository standard and its licence, both kept in the
knowledge base (`fabric-workspace/knowledge/`: `repository-standard.md` rules F1–F11,
`licensing.md`), and make a teammate's README quick start end in a verified MCP call.

## Decision — the licence changes from the next release

**Decided 2026-09-30 by the operator; recorded here because this repository keeps its decisions in
its run records.** From the first release after v0.9.1, Project Observatory is open source under the
GNU Affero General Public License v3.0 only, or available under a commercial licence from
PassionCode.ai: SPDX `AGPL-3.0-only OR LicenseRef-PassionCode-Commercial`, copyright Siarhei
Sheleh. What stays as it was:

| What | Licence |
|---|---|
| v0.8.1 and earlier (tags and wheels) | MIT |
| v0.8.2 – v0.9.1 (tags and wheels) | PolyForm Noncommercial 1.0.0 or PolyForm Internal Use 1.0.0 |
| third-party material (`docs/brand/THIRD-PARTY.md`, the design-system fonts it lists) | its own licence |
| contributions | accepted under [CLA.md](../../../CLA.md), which allows the dual licence |

Why: one licence for every organisation repository, so that anyone may use, study and change the
code, and a closed product or an unpublished modified hosted service needs the commercial licence.
The full wording is the knowledge base's
[licensing](https://github.com/passioncode-ai/fabric-workspace/blob/main/knowledge/licensing.md) page.

## What changed

| Area | Files |
|---|---|
| Licence texts | `LICENSE` is the unmodified AGPL-3.0 text (sha256 `0d96a4ff…abcb0`, the knowledge base template); `COMMERCIAL-LICENSE.md` new, byte for byte the template; `CLA.md` already was |
| Manifests | `pyproject.toml` (`license`, and `license-files` now also carries `COMMERCIAL-LICENSE.md`), both `marketplace.json`, `plugin.json`, the three `SKILL.md` front matters; `SOURCE-INVENTORY.json` refreshed |
| Gates | `tools/check_public_release.py` admits `COMMERCIAL-LICENSE.md` at the root; `tools/check_package.py` admits `licenses/COMMERCIAL-LICENSE.md` in the wheel; `tools/public-identifiers.json` admits `fabric-workspace` (the knowledge base AGENTS.md links) with its reason |
| Test | `tests/test_licensing.py`: LICENSE digest, the commercial licence, the new SPDX in every manifest, no current document calling the product source-available or naming the PolyForm SPDX |
| Docs | README (first paragraph, `## Quick start for a new teammate` with Install / Configure / MCP / Develop, `## Use it`, `## License`), AGENTS.md (`## Read first`, `## Commands`, licence step, `## After work`), CONTRIBUTING.md, docs/HANDOFF.md, docs/PORTABLE-0.1.md, site/index.html, site/llms.txt |

Dated records (`docs/runs/`, `docs/releases/`, `docs/seo/`, `docs/content/`) and the CHANGELOG
sections of past releases keep their wording.

## Checks actually run

On this branch, Python 3.14, a fresh virtual environment with `'.[full]'` from the lock:

- `tests/test_licensing.py` rewritten first and run against the old tree: 7 failures, 1 error
  (PolyForm SPDX in the manifests, non-AGPL LICENSE, missing commercial licence). Green after the
  change.
- `python -m unittest discover -s tests`: 81 tests, OK, exit 0.
- `project-observatory full check`: see the PR; result recorded below.
- `python -m compileall -q observatory`, `python tools/update_inventory.py --check`,
  `pip wheel` + `python tools/check_package.py` (483 archive files, 0 failures; METADATA carries
  `License-Expression: AGPL-3.0-only OR LicenseRef-PassionCode-Commercial` and both license files),
  `claude plugin validate --strict` on the three plugin roots, `docs/site/check.py --self-test`,
  `tools/build_article.py --check`, `node tools/check_site_interactions.cjs`: each exit 0.
- `python tools/check_public_release.py --history --history-ref HEAD`: see below.
- org-index `scripts/check_format.py --offline --repo project-observatory-dashboard` in a scratch
  layout of sibling clones: 6 findings on `main` (F2 F4 F5 F7 F8 F11) → 0 on this branch.

### MCP with a real client

A throwaway `OBSERVATORY_HOME` with one synthetic project, the engine installed from this branch,
and Claude Code 2.1.285 given only that server:

```sh
claude -p "Call the observatory_status tool once. Then reply with exactly: OK <number of projects it reports> degraded=<the degraded list>" \
  --strict-mcp-config --mcp-config mcp.json --allowedTools mcp__observatory__observatory_status \
  --output-format stream-json --verbose --max-turns 3
```

`mcp.json` declared `observatory` as the README's command line (the venv's `python`,
`$(project-observatory full-path)/mcp/server.py`, `OBSERVATORY_HOME`). Observed: `init` reported
`observatory` `connected`; the client called `mcp__observatory__observatory_status` and returned
`OK 1 degraded=[]`, exit 0. The README's MCP step carries the same call.

## Open

- **CHANGELOG.** `AGENTS.md` keeps `CHANGELOG.md` to release pull requests, so this change adds no
  entry. The next release PR adds, under its version: "Licence: AGPL-3.0-only or a commercial
  licence (`AGPL-3.0-only OR LicenseRef-PassionCode-Commercial`); v0.9.1 and earlier keep PolyForm
  or MIT."
- **No release cut for the licence.** The plugin `observatory-log` keeps its version too; its
  manifests carry the new SPDX and ship with its next bump.
- The CLA template's first paragraph still says "source-available licenses"; it is byte for byte
  the knowledge base template (rule F9), so the fix belongs there first.

## Next task

Cut the next release (0.9.2 or 0.10.0, whichever the next change calls for) by the steps in
AGENTS.md → *Releasing*, with the CHANGELOG licence line above in its section; then update the
Project Observatory row of the knowledge base's `products.md` and the org-index
`repositories.json` role text (still "Source-available under PolyForm…") to the released version
and licence.
