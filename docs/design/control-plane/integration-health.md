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

- The filer does not search GitHub, it reads the first hundred labelled issues of a repository; more than that many
  observatory issues in one repository is outside the design.

## Scope, the declared providers, codes (OBS-605)

Built 2026-11-18 on `refactor/r4-doors`, row OBS-605 of wave R4 ([refactor-plan.md](refactor-plan.md)), CP-12 and
CP-13.

### What changed

| Before | Now |
|---|---|
| `TRACKED` was a list kept by hand beside the step (`sentry` counted as tracked while the step asked none of its keys) | each door declares `DOOR_PROVIDER = "<name>"` in its own file, each collector `COVERS_PROVIDERS = (...)`; `tools/door_registry.py` reads them with `ast`, never importing a door, and `tracked_providers` is that set |
| Only OpenRouter, Cloudflare and DigitalOcean keys were asked | Heroku (admin tokens and every authorization in the door's ledger, value from its vault slot) and Sentry (every narrow token in the door's ledger) are asked, and the `gh` session the GitHub collector and the issue filer use |
| No scope check | where the provider answers a key's scope, it is compared with what the collectors and the door's preset need |
| Findings had no code | every `integration.*` finding and every `untracked` row carries a `code` |

### Scope, per provider

| Provider | Asked through | Granted | Needed |
|---|---|---|---|
| GitHub | `gh api -i user` (the existing client: the token never enters this process) | the `X-OAuth-Scopes` header | `repo`, `read:org`; `gist` and `workflow` (what `gh auth login` always asks for) are allowed, no finding |
| Cloudflare, issued tokens | token verify with the token's own value (`/accounts/{id}/tokens/verify`) names its id; the stashed admin reads `/accounts/{id}/tokens/{tid}` | the permission groups of its policies | the preset's `groups`, `zone_groups`, `account_groups` |
| Heroku, issued authorizations | `GET /account` with the value (valid); the admin reads `GET /oauth/authorizations/{id}` | the authorization's `scope` | the preset's `scopes` |
| Sentry, narrow tokens | `GET /api/0/` (validity and scope in one call); a slot is sent only while it holds the value the ledger proved (`_narrow_value_ok`), else `unreadable` | `auth.scopes` | the preset's exact `scopes` |

Admin tokens and OpenRouter and DigitalOcean keys have no scope to compare: no `scope` field on their records.

### Data (added fields only)

- A key record may carry `scope`: `granted`, `needed` (lists, or null when the provider did not say), `missing`,
  `excess`, `state` (`ok`, `missing`, `excess`, `unknown`, `unread`) and `code` (`scope-ok`, `scope-missing`,
  `scope-excess`, `scope-unknown`, `scope-unreadable`).
- An `untracked` row carries `code`: `untracked-key` when a variable (a key) points at the provider, else
  `untracked-package`.
- The document carries `keys_not_asked` (MCP `keysNotAsked`): declared doors whose keys this step does not ask
  (Prowl today).

### Contract

- `Provider(name, holders, ping, scope=None)`. `holders()` may answer (slot, reader, context); `ping(value, context)`
  then gets the context, and returns the provider's answer; `scope(slot, value, answer, context)` answers
  `{granted, needed, allowed?}` or None. `judge_scope` compares: `missing = needed - granted`,
  `excess = granted - needed - allowed`.
- Findings: `integration.scope_missing` (warning, subject `integration:scope/<slot>`, code `scope-missing`) and
  `integration.scope_excess` (warning, `integration:scope-excess/<slot>`, code `scope-excess`). The others gain
  `key-invalid`, `field-drift`, `untracked-key` or `untracked-package`.
- Builders take the door module as an argument (`cloudflare_provider(cf)`, `heroku_provider(hk)`,
  `sentry_provider(sn)`, `github_provider(run, which)`): the doors' own clients make every call, and tests pass
  fakes. `BUILDERS` maps a provider to its builder; `default_providers(degraded)` names a door that cannot be opened
  (`door-unavailable`).

```
door files ─ DOOR_PROVIDER ─┐
collectors ─ COVERS_PROVIDERS ┴─> door_registry.declarations() ─> tracked_providers ─> untracked(...)
                                                     doors() ─────> keys_not_asked
BUILDERS ─> Provider(holders, ping, scope) ─> check_keys ─> key.scope ─> integration.scope_* findings
```

### Failure behaviour

- A declaration in no known shape, or a module that cannot be parsed, is named in `degraded`
  (`declaration-unshaped`, `module-unreadable`) and contributes nothing: the provider then reads as untracked, the
  safe side.
- A scope that cannot be read is `scope-unreadable` on the key, named by the exception's type, and in `degraded`; the
  key's validity stands.
- A provider that did not say (a fine-grained GitHub token answers no scope header) is `scope-unknown`, never a
  clean bill and never a finding.
- No value reaches a record, a finding, a `degraded` entry or argv: `gh` reads its own token, every other value is
  sent by its door's client and dropped.

### Tests

`test_integrations` (suite `integrations`): the registry finds every door and collector, a new door is tracked once
it declares itself, a declaration in no known shape is named with its code, `TRACKED` is the registry's; the
judgement; scope recorded and its findings with codes; a scope that cannot be read; GitHub through a fake `gh`
(scopes header, a 401, no header); Cloudflare verify and policies, Heroku authorization, Sentry auth info through
fake doors, with a value the ledger did not prove never sent; every finding has a code and a label; every fixture
value absent from every answer.

### Not done

- The integrations page shows no scope column yet: the scope findings reach the findings pages and the MCP view.
- Prowl and Google keys are not asked (`keys_not_asked` names Prowl); DigitalOcean token scopes are not compared.
