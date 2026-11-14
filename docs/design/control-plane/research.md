# Public sources and tools (control plane research, part 2)

Read on 2026-11-14 for [CONTROL-PLANE.md](../CONTROL-PLANE.md). Items marked "not verified" were not confirmed on a page
read that day.

## Vulnerability data

- OSV batch query: `POST https://api.osv.dev/v1/querybatch`, body `{"queries":[{"package":{"ecosystem":"PyPI","name":"x"},"version":"1.2.3"}]}`
  (or a purl, never both with a version). Answers in query order, IDs and `modified` only, paginated past 1,000 per
  query. https://google.github.io/osv.dev/post-v1-querybatch/
- Record details: `GET https://api.osv.dev/v1/vulns/{id}`; cache by `modified`. The FAQ states no API limit; HTTP/1.1
  answers are capped at 32 MiB. https://google.github.io/osv.dev/faq/
- Ecosystem names: `npm`, `PyPI`, `Go`, `crates.io`, `RubyGems`, `Packagist`, `Maven`, `NuGet`; the list is
  `https://storage.googleapis.com/osv-vulnerabilities/ecosystems.txt`. Offline export per ecosystem:
  `.../<ECOSYSTEM>/all.zip`, increments in `.../<ECOSYSTEM>/modified_id.csv`. Data licences vary by source (CC-BY-4.0
  for GHSA, PyPI and Go). https://google.github.io/osv.dev/data/
- Version ranges of kind `ECOSYSTEM` need each ecosystem's own ordering, so offline matching is delegated to
  osv-scanner; online, the API matches.
- osv-scanner v2 (Apache-2.0): `osv-scanner scan source -r <dir> --format json`; exit 0 clean, 1 vulnerabilities,
  127 error, 128 no packages; `--offline` with a local database. Reads package-lock, pnpm-lock, yarn.lock, bun.lock,
  poetry.lock, uv.lock, pdm.lock, pylock.toml, Pipfile.lock, requirements.txt, Cargo.lock, go.mod, Gemfile.lock,
  composer.lock, pom.xml, gradle.lockfile and more. https://google.github.io/osv-scanner/supported-languages-and-lockfiles/
- Known exploited: `https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json`; join an OSV
  record's CVE alias to `cveID`. https://www.cisa.gov/known-exploited-vulnerabilities-catalog
- GitHub's synchronous SBOM endpoint was due to end after 2026-11-13; the asynchronous pair (`generate-report`,
  `fetch-report`) replaces it. https://docs.github.com/en/rest/dependency-graph/sboms

## Lockfiles readable with the standard library

- JSON: package-lock v2 and v3 (`packages` map, skip `link`), Pipfile.lock, composer.lock.
- TOML (`tomllib`): Cargo.lock, poetry.lock, uv.lock (its schema is the tool's own; treat it as unstable), pylock.toml.
- Text: go.mod `require` blocks (go.sum over-approximates), Gemfile.lock `GEM/specs`, requirements.txt `==` pins.
- YAML formats (pnpm-lock, yarn Berry) need a parser the standard library lacks: delegate to osv-scanner or read the
  narrow subset the format uses. Not verified: every yarn v1 variant.

## MCP servers, skills and plugins

- Snyk Agent Scan (formerly mcp-scan, Apache-2.0): checks prompt injection, tool poisoning, shadowing, toxic flows in MCP
  servers and risky skills; sends tool definitions and skill text to the vendor's API and needs its token.
  https://github.com/snyk/agent-scan
- Cisco mcp-scanner (Apache-2.0): YARA and regex analyzers run locally; LLM, vendor and file-reputation analyzers need keys;
  `static` mode reads dumped tool lists without starting servers. https://github.com/cisco-ai-defense/mcp-scanner
- Description pinning: hash the whole canonical tool definition (name, description, input schema), not only the top
  description, or a changed parameter description passes. https://github.com/snyk/agent-scan
- Attack classes to check offline: hidden Unicode and concealment directives in SKILL.md, CLAUDE.md and AGENTS.md;
  bundled scripts that read credentials and send them out; a shipped `.mcp.json`; hooks that run shell on every tool
  call; unpinned package runners in an MCP command line; fetch-and-execute; a skill changed after approval.
  https://arxiv.org/html/2602.06547v1

## Key scanners

- gitleaks (MIT, security fixes only since 2026-05; successor Betterleaks, MIT, command-compatible):
  `gitleaks git|dir -f json -r <file> --redact --no-banner`; even redacted, drop `Secret`, `Match` and `Line` before
  storing. https://github.com/gitleaks/gitleaks
- TruffleHog (AGPL-3.0): its verification sends the candidate to the provider; use `--no-verification` only, and strip
  `Raw` and `Redacted`. https://github.com/trufflesecurity/trufflehog

## Filing issues

- Limits: 5,000 requests an hour; at most 80 content-creating requests a minute and 500 an hour; writes serial and at
  least one second apart; honour `retry-after`. https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
- Deduplicate with a hidden fingerprint marker in the body and a label, and keep a local fingerprint map; search allows
  30 requests a minute. Never a key value in an issue. https://docs.github.com/en/rest/search/search
