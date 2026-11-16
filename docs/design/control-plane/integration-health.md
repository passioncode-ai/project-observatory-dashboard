# M8: integration health and issues (OBS-557)

Status: built, 2026-11-16. Requirements CP-12, CP-13 and CP-14 and the contracts `integration.*` findings and the
issue fingerprint map of [CONTROL-PLANE.md](../CONTROL-PLANE.md). Public sources: [research.md](research.md),
"Filing issues". Scenario OSS-123.

## What

| Piece | File | Runs |
|---|---|---|
| Key validity, drift, untracked | `observatory/engine/tools/integration_health.py` | every tick (`integration-health`), asks each key at most once a UTC day; `full integration-health [--force]` |
| Findings | `observatory/engine/tools/integration_findings.py`, called by `build_findings.py` | every findings build |
| Issue filer | `observatory/engine/tools/issue_filer.py` | every tick after `findings` (`issues`); `full issues [--dry-run]` |
| Page | `observatory/engine/dashboard/integrations_page.py` (`integrations.html`, Monitoring, after Availability) | every dashboard build |
| Notice | `integrations_page.banner_html`, also on Home (`home_page.issues_notice_html`) | until dismissed |
| Agent views | MCP `observatory_integrations`, `observatory_issues` | on request |

## Data

- `store/raw/integration-health.json` (`integration-health/1`): `keys` (`provider`, `slot`, `state` valid, invalid,
  unreachable or unreadable, `status`, `checked_on`, `since` for an invalid key), `drift` (`collector`, `field`,
  `last_seen`, `rows`), `untracked` (`project`, `provider`, `category`, `evidence` of kind package or env and a
  NAME, `evidence_total`), `tracked_providers`, `degraded`.
- `store/state/integration-fields.json`: collector to field to the last UTC day a row carried it.
- `store/state/issues.json` (`issues/1`): `issues` (fingerprint to `repo`, `number`, `state` open, closed or
  not-filed, `reason`, `type`, `subject`, `filed_on`, `closed_on`), `last_run`, `banner.dismissed_on`.
- A value is read into memory for one request and dropped. Nothing stored, logged or sent names a value: a slot, a
  package or a variable is named, never its content.

## Contract

- **Key validity (CP-12).** `Provider(name, holders, ping)`: `holders()` lists (slot, reader of the value) and
  `ping` is the door's own read (OpenRouter `GET /keys`, Cloudflare `GET /accounts` then `/memberships`,
  DigitalOcean `GET /v2/account`; the calls the doors' `ping` verbs make, so no new spend). `HTTP 401` or `403` in the
  door's error is `invalid`; any other failure is `unreachable`, which says nothing about the key. A key already
  checked on the UTC day keeps its record. Offline (`OBSERVATORY_OFFLINE=1`) asks nothing and keeps the last answer.
- **Drift (CP-12).** `COLLECTOR_FIELDS` declares per collector the stored raw file, the list of rows and the fields
  every healthy row carries. A field absent or null in every row of a non-empty list is `drift`. No file, an
  unreadable file or no rows say nothing.
- **Untracked (CP-13).** `PACKAGE_PROVIDERS` (fnmatch on the lower-case package name, any ecosystem) and
  `ENV_PROVIDERS` (on the upper-case variable name) map to a provider and a category (payments, errors, cloud,
  analytics, messaging, ai). `TRACKED` lists providers with a door or collector. One row per project and untracked
  provider, at most five evidence names.
- **Findings.** `integration.key_invalid` (warning, subject `integration:key/<slot>`), `integration.drift` (warning,
  `integration:drift/<collector>/<field>`) and `integration.untracked` (info,
  `integration:untracked/<project>/<provider>`, carries `project`). All three are in the Security group.
- **Issue filer (CP-14).** Classes: `dep.vulnerable` and `integration.*` on by default; key findings (`credential.`,
  `secret.`, `env.`, `ssh.`) off by default. Switches in the configuration (door `observatory_configure`):
  `integrations.github_issues` (all) and per class `github_issues_dependencies`, `github_issues_integrations`,
  `github_issues_keys`. A project's repository comes from its `implemented_by` relation. A key finding
  (`integration.key_invalid` and the key classes) is never filed unless the repository is private. A finding's
  fingerprint is the first 16 hex of the hash of its type and subject, written into the issue body as
  `<!-- observatory-fingerprint:... -->` with the label `observatory`; before filing, the repository's labelled issues
  are read once per run and a marker found there is adopted, so a lost map files nothing twice. Writes are serial, one
  second apart, at most `CAP` (10) creations or closings a run (GitHub allows 80 creating requests a minute and 500 an
  hour); a 403 or 429 stops the run and records `retry_after`. A closed finding gets one comment and the issue is
  closed; the same finding returning after that gets a new issue. The body goes to `gh` on stdin, never in argv.
- **Notice.** `banner.show` is true while the filer is on and `dismissed_on` is empty. Dismissal: the key server action
  `issues-banner` (`op: dismiss`) and `observatory_issues(dismissBanner=true)`; both store the day.
- **MCP.** `observatory_integrations(state?)`: keys (invalid first), drift, untracked, `checkedOn`, `degraded`.
  `observatory_issues(state?, dismissBanner?)`: the map, `lastRun`, `banner`, `degraded`.

## Wireframe

```
Integrations
Are the held provider keys still accepted, did a provider change an answer, and which issues were filed.
[Issues are filed for findings. ... To turn it off, switch off github_issues ...  [Dismiss]]
Checked 2026-11-16
[Keys checked 3 | Answers that changed 1 | Providers nothing watches 1 | Issues open 1]
cloudflare:  key | state | answer | checked | since   (refused first)
Answers that changed shape: collector | field | last seen | rows read
Providers nothing watches: project | provider | kind | seen as (names)
Issues filed: issue | state | finding | filed | why not      Last run: 1 filed, 0 closed, 0 waiting
[Not fully read: source . reason]
```

## Failure behaviour

- A door that cannot be imported or cannot list its keys is named in `degraded` (`integration:<provider>`); the others
  still run. A key whose value cannot be read is `unreadable` and is never asked.
- No write access, issues disabled, no project, no repository, a repository not on GitHub, a public repository for a key
  finding, or a body that `credential_shape` reads as a credential: `not-filed` with the reason, retried at the next
  run, never an error.
- A host refusal other than a rate limit stops the run with `host-error`; the map keeps what was done.
- A lost or unreadable map is read as empty; the markers in the repository prevent a second issue.
- `gh` not signed in: a dry run, which writes nothing and says what it would do.

## Tests

`test_integrations` (a 401, a 403 and a renamed field fixtures, once a day, the mapping table, no value stored),
`test_issue_filer` (a fake host: dedupe by marker and map, serial writes and the cap, rate limit, close, key findings
in a public repository, no value in a body, the `gh` host sends the body on stdin), `test_integrations_page` (page,
notice, key server action), `test_mcp_wire` (the two tools), `test_agent_parity`, `test_i18n`.

## Not done here

- Heroku and Sentry keys are not asked in this change: only the OpenRouter, Cloudflare and DigitalOcean doors are
  wrapped; `default_providers()` is the one place to add another.
- Token scope against what the collectors need (the third half of CP-12's wording) is not checked; a key whose scope
  is too narrow shows as drift once the collector stops receiving the field.
- The filer does not search GitHub, it reads the first hundred labelled issues of a repository; more than that many
  observatory issues in one repository is outside the design.
