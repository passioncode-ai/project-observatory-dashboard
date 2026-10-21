# Changelog

All notable changes to Project Observatory. Versions follow [semantic versioning](https://semver.org/);
while the major version is 0, a minor release may change behaviour and says so here.

## Unreleased

### Changed

- **google-auth 2.62.0** in the `[full]` pins and both lock copies (supersedes Dependabot #324, which moved the pin
  alone and could not resolve against the lock).

### Fixed

- **Every collector gets a turn, and none takes the tick** (OBS-52; OBS-40 superseded). One installed 0.27.0 timed
  out on 28 of its last 50 ticks: the leak scan took a median 1125 s of the 1500 s ceiling and eight collectors were not
  started 28 times. A disabled step keeps its old start, so it sorted as the most starved collector and the rotation
  moved the step it reads from to the front: the disabled companion scrub dragged the leak scan, and the disabled
  Bitbucket listing the folder scan, to the head of every tick. A disabled collector now runs last, one not started for
  3 ticks runs first, and each collector's watchdog limit is at most its share of the ceiling (40 %, the leak scan 30 %;
  `tick_budget.py`). The leak scan also stops between the chunks of a file and the pages of a store and resumes there
  next tick, instead of being killed with nothing written, and a complete pass over its databases (forced whenever the
  set of known values changes, which is most days) now resumes across ticks from how far it read each table, the
  databases it has not finished first; before, each tick began every database at rowid 0 in the same order, and one
  large store kept the ones behind it unread. A database finished earlier in such a pass is read incrementally, with
  its signature, so a row updated in place meanwhile is still read; an unfinished one resumes only if it is the same
  file; a pass pending more than 2 days starts over; and a database that will not open is named as a hole without
  holding the pass open. A new finding, `tick.step_over_share`, names a step that used 95 % of its share of the
  ceiling in 3 of the last 5 ticks (a step that resumes by design, the leak scan, is not judged).
- **A probe's "no" is not a failed step, and a crash is not a "no".** `backup-due` answered "not due" with exit 1, and
  17 tick receipts in a row recorded it as `failed`. "Not due" is now exit 3 (`backup_store.NOT_DUE`, recorded as
  `not_due`), anything that goes wrong while answering exits 2, and any other exit (an uncaught exception gives 1) is a
  failed step whose output goes to the tick log. A test refuses a `bounded(...)[0] == 0` in `tools/tick.py`.
- **Findings handed to Fabric Dashboards are not new again on the next tick.** With the host present the notifier
  returned before recording what it handed over, so every tick reported about ninety findings as new or escalated.
  They are now recorded as delivered `via: host`.
- **A degradation that lasts a day is a warning** (`collector.degraded_persistent`). A collector whose same reason
  (its `code`, else its text) stays in its receipt for more than 24 hours is reported on its own, for every collector
  but the structural reasons `degradations.STRUCTURAL` names, by source and reason together (the leak scan's coverage,
  kept off the push channel; the disk guard's holder policy; retired keys; one checkout's or folder's own state;
  listing budgets), or a row marks `structural: true`; a real failure from the same source still fires:
  the Fabric attribution collector had written `degraded: access-refused` on every run for over a day and produced
  nothing, while the board showed it as one more info row. First sightings are kept in the store, `degraded-since.json`.
- **`tick.err` lines carry their time.** The supervisor's lines (`tick stopped: ...`) and the tick script's stderr now
  start with their UTC time, through `log_policy.StampedLines` (moved from `serverd.py`, which still uses it).

- **A machine restore never starts a background `git gc`** (OBS-157 follow-up). Every git command `full machine-restore
  --clone` runs now carries `-c gc.auto=0 -c maintenance.auto=false`, as the engine's other git readers do: a fetch of
  carried commits into a checkout that was already there could leave a detached `gc --auto` running after the
  restore ended, writing into a repository a person owns (and, on Linux CI, into a test's temporary folder).
- **No test, and no git the launcher runs in its own sample repository, leaves background maintenance behind**
  (OBS-157 follow-up, the class behind the refused v0.27.0 tag). Every test's git environment now comes from one helper,
  `observatory/engine/tests/git_env.py`, which shuts out the system and global configuration and appends `gc.auto=0`
  and `maintenance.auto=false` through `GIT_CONFIG_COUNT` after any entries a caller set (an inherited
  `GIT_CONFIG_PARAMETERS`, which git ranks above them, is dropped); the portable runner's `clean_env` and the
  twenty-two test files that built their own git environment use it, and check.yml's workflow-level `env:` carries the
  same entries for every CI step, so the root tests (run by `unittest discover`, outside the runner) are covered at
  run time too. `observatory demo` commits into its sample repository and `observatory scan` reads repositories with
  `-c gc.auto=0 -c maintenance.auto=false`, as the engine's `safe_git` already did.
  `tests/test_git_background_maintenance.py` fails on a test that sets or removes a `GIT_CONFIG_*` variable itself,
  strips every `GIT_*` variable by hand or hands git an environment built as a literal dict; on a workflow `env:`
  that differs from what the helper composes; and on any git command the engine, the launcher or `tools/` spell
  (a list, a concatenation, `args=`, `create_subprocess_exec`, a shell string, or a binary bound to another name)
  that is not a pure read and lacks both settings among git's own `-c` options. On GitHub Actions it also asks git
  what it reads in the root tests' own process; `test_git_env` does the same for the runner's environment.
- **An OpenRouter consumer's key past every listed page is asked about itself.** On an account where another product
  mints a key per user (about 300 a day, measured 2026-10-21), the gateway's key sat beyond the newest 21,200 keys
  the deep walk reads, and every run reported its existence as unknown. A consumer's key the listing and its
  remembered hash did not settle, while the listing is partial, now asks `GET /key` with that key alone before the
  account is walked: a matching label from the provisioning key's workspace verifies it and its answer (label, limit,
  usage) becomes its row; another workspace is reported as such; a 401 is an invalid, disabled or deleted key;
  any other answer stays unknown with its reason. A complete listing that lacks a key still wins. Every request
  refuses a redirect (urllib would carry the key's `Authorization` header to the new location), and a `.env` is
  read only for the exact `OPENROUTER_API_KEY=` variable.

## 0.27.0 (2026-10-20)

Two new provider capabilities, both data-private: the DigitalOcean door gains a team side (stash a team's token, list teams
and members, install, rotate and revoke narrow tokens) and a collector of what each team runs and costs, and a machine can
enroll in an organization server and send it signed activity (`full org`, contract CT-65). The Cloudflare door gains two
zone presets (cache purge, Email Sending onboarding), and `google-auth` moves to 2.61.0 with its lock.


### Added

- **Organization enrollment: `full org`** (OBS-161; contract DEC-0031, DEC-0056, DEC-0082, DEC-0086). A machine
  enrolls in an organization server with a single-use enrollment token read from a vault slot (never from the command
  line, never stored or logged), with device keys of its own for each organization (declared as an owner-only key
  file, `storage: file`) and a certificate request signed by that `#sig` key; it keeps the certificate,
  stream epoch and accepted protocols under `node/orgs/<org id>/`, holds one lock per organization so no two commands
  (or a command and the maintenance tick) interleave, rotates the certificate at two thirds of its life (a lost answer
  resends the same request),
  checks in, and sends activity batches from a durable outbox (persisted events, contiguous slices of at most 1000,
  one `batchId` across retries, backoff, acks consumed, overflow bounded). Every request after enrollment carries the
  `fabric-device` HTTP signature. Share registrations are derived from the estate revision and delta this machine
  verified and applied (`org records apply`) and sent only to a server that lists `share-registration/1`;
  `grant.usage` for a share grant goes only to its own organization, only while the registration answer allows it
  (checked again at send), and only once none of the day's calls can still settle. The maintenance job runs `org tick` on an enrolled machine. `org status` names expiry, the last check-in, the
  outbox depth and anything degraded; `org leave` removes the local enrollment only. The contract's
  `http-signature.json` (54 cases) and `share-registration.json` (derive, grant id and gate cases) are vendored and
  run.

- **DigitalOcean teams in the DigitalOcean door, and a collector of what each team runs and costs** (OBS-155).
  `tools/digitalocean.py` gains an account half beside its database half, symmetric to the Cloudflare door:
  `stash` (a team's own token on stdin, or `--from-doctl CONTEXT|all` reading doctl's config directly, nothing
  printed), `teams`, `members --team`, `team create` (plans by default, writes with `--apply`), `presets`, `issue`,
  `install`, `list`, `ping`, `rotate` and `revoke`. Team tokens live in `secrets/digitalocean-admin/` (0600), the
  collector's read tokens in `secrets/digitalocean-read.d/`, and a record per installed token in
  `secrets/digitalocean-issued/` (team, preset, slot, declared scopes, dates; never a value).
  - What DigitalOcean's public API cannot do, measured on 2026-10-19 and handled rather than faked: it has no
    endpoint to create, roll or list personal access tokens ([API reference](https://docs.digitalocean.com/reference/api/digitalocean/);
    `GET /v2/tokens` answers 403 to a full-access personal token), so `issue` refuses with the control-panel recipe
    (the preset's [custom scopes](https://docs.digitalocean.com/reference/api/scopes/)) and `install` takes the minted
    token on stdin, checks its team and its preset's reads, then delivers it. It has no endpoint to list, invite,
    remove or re-role members; `members` says so, and `team create` is the one documented write
    (`POST /v2/organizations/team`, in an organization context). Revocation uses the documented
    [OAuth revocation endpoint](https://docs.digitalocean.com/reference/api/oauth/) and counts only when
    `/v2/account` then answers 401 to the old value.
  - `collectors/scan_digitalocean.py` (`full scan-digitalocean`, daily in the tick, behind
    `integrations.digitalocean`) writes `store/raw/digitalocean.json`: per team the balance and month-to-date usage,
    Droplets with their monthly price, App Platform apps with the repositories they deploy from, databases without
    their credentials, volumes and domains. It reads with the team's `observatory-read` token when one is installed,
    else its stashed token, and says which. A 401 (the team), 403 (a resource) or 429 (a resource, with the wait) is
    named in `degraded`; the rest still lands, as does every row but a malformed one, and a team not
    reached before the step's deadline is named while what was read is written.
  - What the door refuses: a team's own stashed token, or any token that can read outside its preset's
    scopes, installed as a narrow token; an install over a label already installed (`rotate` replaces it and
    revokes the old one); a value with whitespace or a control character, before any header is built. `revoke` of
    a token whose value is gone claims nothing and keeps the record (`revoke LABEL --forget` drops it, saying no
    revocation was observed). Install, rotate and revoke hold one lock beside the records.
  - Two findings: `digitalocean.droplet_off_billed` (a powered-off Droplet is billed in full until destroyed) and
    `digitalocean.app_down` (an App Platform app with no active deployment; its project named only through a
    repository the registry already ties to one), plus `digitalocean.snapshot_stale` past two days.

- **A Cloudflare preset that onboards a domain to Email Sending** (`email-sending-domain`). Until a zone's domain is
  onboarded, an account delivers only to its verified Email Routing destinations and refuses every other recipient
  with `10203 email.sending_disabled`, whatever the Workers plan. Onboarding is a zone endpoint
  (`POST /zones/{zone}/email/sending/subdomains`) that refused the account-wide token shape with 401. The preset grants
  Zone Read and DNS Write on the zone's own resource plus Email Sending on its account, as two policies, verifies the
  new value by listing the zone's sending subdomains, and delivers it to a vault slot:
  `cloudflare.py issue --preset email-sending-domain --zone example.com --vault PROJECT/ENV/NAME`.

- **The Cloudflare door issues a cache-purge token for one zone** (`issue --preset cache-purge --zone <zone> --vault
  PROJECT/ENV/NAME`): `Zone Read` and `Cache Purge` on that zone only, named after the zone and the slot. Listing a
  zone proves nothing about purging it, so the proof is a purge of one URL the site does not serve
  (`/.well-known/observatory-purge-probe`), never `purge_everything`; a zone preset's probe body may now name the zone.
  First holder: a landing site that purges its zone after each CI deploy.

### Changed

- **Dependencies:** `google-auth` 2.61.0 (from 2.59.1) in the `[full]` extra, with `requirements-full.lock` and its
  engine copy `observatory/engine/requirements-full.lock` moved to the same pin, so `pip install -c
  requirements-full.lock '.[full]'` resolves (supersedes Dependabot #283, which bumped `pyproject.toml` alone). No other
  pin moves.


## 0.26.0 (2026-10-20)

The companion's Stop hook records and asks about what a turn changed, not what the working tree already held: a
session's baseline is taken at SessionStart, standing untracked files never count, and the `why` is asked only for
tracked content. A session's MCP server moves itself onto the installed release instead of answering `stale-server`.
New on this line: a daily encrypted backup of the machine's agent setup with a restore onto a new machine, a Heroku
door with team management and the team side of the Cloudflare door, Fabric's project id on every project, and the
delegation verifier and meter brought to the contract's DEC-0045. The companion plugin `observatory-log` moves to
0.26.0.

### Security

- **The delegation verifier follows the contract's DEC-0045 ("Delegation sealed")** (OBS-162). `delegation.py` was
  a port of fabric-agent-contract `src/delegation.ts` at `8e49fbd`, which predates it; it now follows the contract at
  `07042181`, and `tests/test_delegation_sealed.py` ports `test/delegation-sealed.test.ts` case by case over the
  contract's own fixtures (`fabric/delegation-vectors/`).
  - **A small-order Ed25519 key is refused** (CT-29, critical). A key set publishing one of libsodium's seven
    small-order encodings let anyone forge a token with no private key: the contract's vector
    `delegation-small-order.json` verified as `ok`. The key is now the issuer's unusable key,
    `issuer-unavailable` (`small-order-key`), before anything is verified under it. The same predicate
    (`is_small_order_key` in the engine's `ed25519_order.py`, which `node/ed25519.py` re-exports) now guards the release-signature check as well, in both its
    `cryptography` and its plain-Python path.
  - **The trusted issuer is required.** `verify_delegation` and `verify_meter_token` take `issuer` with no default;
    none or empty is `issuer-unavailable` (`no-trusted-issuer`), never "any key in the set".
  - **The meter's token is exact**: scope exactly `grant.check` (`meter-scope-not-exact`) and effect exactly
    `["none"]` (`meter-effect-not-none`); a wider token is `principal-unproven`.
  - **Strict types in the contract's order** (DL-13 row 5): actors' agent ids and session ids, URIs for `iss`,
    `sub` and `project`, `aud` an agent id, the `scope` and `jti` grammars, each effect once, one grant, `spend`,
    `task` and `nbf` are checked before the lifetime, audience and depth rows, so a malformed chain is
    `unauthenticated` whatever else is wrong.
  - A published key of another `use` or `alg` is now `issuer-unavailable` (`key-not-for-signing`), not
    `unauthenticated`.
  - Every grammar (agent ids, session and task ids, URIs, `scope`, `jti`) must match the whole value: Python's `$`
    let a trailing newline through where the contract's regular expressions do not. The same full-match rule now
    applies to the meter's request and grant-reference checks and to the agent-network node's identifier,
    envelope, passkey, estate and blob checks. `NaN` and `Infinity` in a token are `malformed`, a signature segment
    no base64url encoder writes is `bad-signature` (it raised before), and an integer-valued time such as
    `1791720000.0` is read as the integer the contract reads.
  - White space in a grammar is ECMAScript's, as in the contract: a URI holding U+FEFF is refused, one holding
    U+001F or U+0085 is accepted, and the meter's timestamps and the node's relay origins take ASCII digits only.
    An integer no double can hold (2^1024 - 2^970 and above, 309 digits or more) in a time or an amount is a claim
    of the wrong type (`malformed-claims`), as the contract reads the infinity `JSON.parse` makes of it.

**Behaviour change for existing installs.** A meter that verified tokens without a trusted issuer now denies every
grant check and every token-bearing slot or keyserver call as `principal-unproven`
(`issuer-unavailable:no-trusted-issuer`). An install with no `config/meter.json` and no hub keys denied them before
too (no estate keys); nothing changes there. If `config/meter.json` names keys (`jwks` or `hubJson`) but no issuer,
add the estate issuer Fabric names:

```json
"issuer": "urn:fabric:estate:<id>"
```

or point `hubJson` at a `hub.json` that names its `issuer` beside its `jwks`; the meter takes the hub's issuer only
with the hub's own keys. `full doctor` reports it under `meter` (`degraded`: `meter:no-trusted-issuer`, and the line
that fixes it), and `full grant --status` lists it in `degraded`. The contract defines no default issuer, so the
meter sets none: trusting whichever key signed would be the forgery this fix closes.

### Added

- **A daily encrypted backup of this machine's agent setup, and a restore onto a new machine** (OBS-157).
  One `machine-<stamp>-<id>.obmachine` per run goes to the same backups root as the workspace snapshots, in the same
  `OBSENC1` format and under the same passphrase: the Claude Code and Codex configuration, Git, SSH and shell
  settings, CLI logins, launchd or systemd user jobs, the Fabric service descriptors, the Homebrew, npm, pipx and uv
  inventories, and every checkout's remotes (without user information), branch, HEAD, dirty and ahead/behind state
  with a git bundle of every commit no remote has (branches, local tags, detached HEADs, stash entries), a patch of
  its uncommitted changes (staged changes for a repository with no commit yet) and its untracked files. The
  maintenance job takes one a day, `full update` one before it stops the jobs (unless one is under an hour old), and
  `full machine-backup` one now. `config/machine-backup.json` adds an operator's own paths, excludes and checkout
  roots. `full machine-restore [BUNDLE | --latest]` prints the plan; `--apply` writes only what is missing,
  `--overwrite` moves a conflict aside first, `--packages` installs the inventories and `--clone` clones the checkouts
  and puts back their carried work. Measured on a working agent machine: 69 MB, 14,221 items, 517 checkouts, under
  two minutes.
  - Decrypted and temporary copies live only in the workspace's private `store/.machine-stage/`, one restore
    step at a time; SIGTERM and SIGHUP remove them and the next run sweeps what a killed one left; a run refused while another
    holds the stage touches nothing. A checkout not restored is named and `full machine-restore` exits 1.
  - Nothing is written without a passphrase; a disk that would keep under 2 GB free refuses the write, recorded and
    shown in `doctor`, `maintain status` and Health; three bundles are kept.
  - What a cap leaves out is not carried and named per folder in the bundle's `MANIFEST.json`, the answer, `doctor`
    and Health; transcripts (opt-in) and plugin caches are skipped and named. No value reaches the manifest, the
    state file or an answer.
  - `doctor` warns when the newest bundle is older than 36 hours; an update never fails on its machine backup.
    Guide: [ONBOARDING, This machine's agent setup](docs/ONBOARDING.md#this-machines-agent-setup-and-moving-to-a-new-machine).
    Scenario OSS-70.

- **A Heroku door, and the team side of the Cloudflare door** (OBS-156). `tools/heroku.py` does for Heroku what
  `tools/cloudflare.py` does for Cloudflare, on the Platform API:
  - `stash` keeps an admin token read by the door alone: the CLI session's, piped in (`heroku auth:token | heroku.py
    stash`) or read inside the process with `--from-cli`, never in argv.
  - `issue --preset identity|read|deploy|config-read|config-write --vault PROJECT/ENV/NAME [--team T] [--app A]
    [--expire-days N]` mints a direct OAuth authorization with that preset's scopes only (never `global`), described
    after its preset, slot, team and app. It proves the new value can read the app, the team's apps or the account,
    then writes it to the slot on stdin. A failed proof or vault write deletes what the call created. Issuing again
    regenerates the same authorization; a changed preset makes a new one and deletes the old only after delivery.
  - Heroku narrows an authorization by scope only, never to a team or an app, so `--team` and `--app` are its stated
    purpose and what the proof reads; every issue says so. The `config-` presets reach config vars and need `--app`.
  - `rotate` regenerates in place (`--leaked` for every slot an open leak names), `revoke` deletes at Heroku before the
    ledger row, `list` and `ping` report from `heroku-issued.json` (ids, scopes, slot, team, app, dates; never a value
    or the refresh token), and `ping` names a gone or widened authorization and managed strays.
  - `teams`, `members --team` and `apps --team` read every page, with the slots issued for each team and app.
    `member add|role|remove --team T --email E [--role admin|member|collaborator]` prints its plan and changes nothing
    until `--apply`, then reads the team again to verify. It refuses `owner`, the last admin and the stashed admin's
    own membership.
  - `tools/cloudflare.py` gains `accounts` (every stashed account, the tokens Cloudflare holds in it and the vault slot
    each managed one names), `members --account`, and `member add|role|remove --account` with the same plan, `--apply`
    and verification. It reads Super Administrator from both places it lives, legacy roles and permission policies,
    and refuses: any write while some member's Super Administrator state cannot be read (a record with neither
    roles nor policies, a role or permission group without a name, a policy scope it cannot read); removing, or
    changing the role of, a member who holds Super Administrator unless another accepted member holds it on the
    whole account (a policy scoped to a zone or to some objects does not count); a role change of a member whose
    access is set by policies, since it writes legacy roles only; and the stashed admin's own membership when the
    token can read its user. Lists are read to their end whether they report `total_pages`, `total_count` or
    only a page size, and one still going after 100 pages is an error, not a cut.
  - A team that takes people by invitation (its `team-invite-acceptance` feature on) is written the Heroku CLI's
    way: `member add` sends an invitation, `members` lists pending invitations as pending, `member remove` of a
    pending invitee cancels the invitation, and the read-back that verifies such a write is the invitations list.
    A feature list that cannot be read refuses the write rather than guess the endpoint.
  - `issue`, `rotate`, `revoke` and `ping` hold one lock beside `heroku-issued.json` from their first read to
    their last write, and each write replaces only its own row, so concurrent runs keep every row.
  - `issue` proves every target it names (the app and the team's apps when both are given) and prints exactly
    what the new value read; a team or app the scope cannot read (an `identity` value) is said to be unproved.
- **Fabric's project id on every project** (OBS-160; Fabric CO-331, fabric-agent-contract DEC-0089). Fabric owns
  the canonical project id. A new tick step, `fabric-attribution` (and `full fabric-attribution`), asks Fabric's
  hub `fabric.project.resolve` for every folder the registry holds and records `fabric:<uuid>` on the project in
  `store/raw/fabric-attribution.json`, with its provenance (Fabric, the hub's pid, the time) and a history of
  every addition, replacement and withdrawal. The hub is reached only at the origin `hub.json` names, only on
  `127.0.0.1`, never through a proxy, and only while its pid is alive and is the process listening on the
  hub's port as this user since before the hub started; access is asked once with `fabric.access.request`, and the binding
  credential stays in an owner-only file and goes nowhere else. A project whose folders resolve to two Fabric
  projects is the finding `fabric.attribution_conflict`, never a merge. An unreachable hub is named in
  `degraded` and changes nothing recorded.
- **`project.resolve` answers `fabric:<uuid>` and absolute folders.** A folder is attributed by the contract's
  folder rules (`folder_attribution.py`, a port checked against all of the contract's vectors), with the `_*`
  folders under the projects root and `config/folder_exclusions.json` as declared containers. Both answer
  with `matched.via` `alias`, so the published output schema is unchanged.

### Changed

- **The SessionStart hook asks Fabric which project a session's folder is**, then the contract's folder rules
  while Fabric is not running, and only then the older registry match. Observatory no longer decides a session's
  project on its own rules first.

- **A session's MCP server moves itself onto the installed release** (OBS-159). After an update, every open
  session's Observatory server answered `stale-server` until a person restarted that session, and the
  maintenance job installs releases by itself. Now the server notices at its next request that its code was
  replaced, holds that request, lets the running ones answer, starts the installed release once to check it
  (`mcp/server.py --reload-check`) and replaces itself in place with `os.execv`: same process id, same pipes,
  same session. The new release takes over the client's handshake without a second `initialize` and answers
  the held request exactly once; a changed tool set is announced with `notifications/tools/list_changed`
  (the server now declares `tools.listChanged`). The session's stdio is served by `mcp/stdio_relay.py`
  instead of the SDK's own transport on POSIX.
  - Where the move is impossible the server keeps answering `stale-server`, now with the reason: Windows,
    an update holding the workspace or requests still running after a minute (both retried at a later
    request), an installed release that fails its check
    or speaks another MCP revision, code that is gone, or a failed `execv`. The pipe stays open and no
    request is answered twice.
  - Each move and refusal is one line in `store/logs/mcp-reload.jsonl` and on stderr (from, to, pid,
    reason); `full doctor` lists them under `session_servers`, `full update` says what now happens to the
    servers it lists, and the lifecycle scan no longer reports a server that moved as running replaced code.
- **The Stop hook records and asks about what the turn changed, not what the tree holds.** The recorder took every
  `git status` entry as the turn's work, so standing untracked files (a pasted log, a `.worktrees/` folder) made
  every turn "changed": measured 2026-10-20 on one machine, every recorded turn of 21 sessions was asked about, and
  one session was asked 35 times about the same "4 files changed, +0/-0" it had not touched.
  - SessionStart keeps a snapshot of the checkout as the session's baseline (`tools/turn_state.py`); the record's
    files are those that differ from it, and a turn that changed nothing since the previous turn's snapshot writes
    nothing. A session without a baseline takes the untracked files it first sees as already there.
  - The `why` is asked only when the turn changed tracked content (an edit, a staged or intent-to-add file, a
    deletion, a commit). A new untracked file is recorded but not asked about until it is added.
  - The corroborator leaves a session record whose newest revision is under six hours old to settle
    (`settling` in `store/raw/corroboration.json`): promoting a live record made the session's next changed turn
    start another record and ask again, one record per tick.
  - The recorder's git calls share an 8-second budget and skip dirty submodule trees
    (`--ignore-submodules=dirty`; `git status` went from 0.45-1.9 s to 0.01-0.3 s in a checkout with eleven
    submodules); a turn that runs out is unrecorded, silently, and counted at the next turn. The hook parses its
    payload once instead of starting the interpreter once per field.

## 0.25.0 (2026-10-19)

The recovery work of 2026-10-13 lands on this line. Cleanup keeps local work it cannot verify, rechecks every target
just before removal and refuses when it cannot tell whether a process holds a path. The machine survey records what
it could not measure: checkout state and upstream relation under explicit roots, cache coverage, open files whose
path is gone and memory pressure apart from swap, each read-only and none of it cleanup authority. Each tick stage
leaves a receipt, and an offline check passes only on tests that actually ran. 0.25.0 reclaims less disk than
0.24.5: with holder coverage unknown, nothing that needs a holder check is removed. On the main line since 0.24.5:
each project's OpenRouter spend, the agent-network node, SSH keys and external checkouts declared by SessionStart.
The companion plugin `observatory-log` moves to 0.25.0: it asks for a turn's `why` once per record, with the next
prompt, and measures the answers.

### Added

- **`registry/agents.json`: the agents installed on this machine** (L-11, OBS-83, #288), generated from the Fabric
  service descriptors, each one's manifest, launchd's job list and the MCP inventory by origin, and projected into
  the wiki unchanged. No token path, no credential-shaped URL and no absolute home path reaches either copy.
  - A home written in an equivalent lexical form (doubled separators, `.` segments, `x/..` detours, backslashes,
    JSON and percent escapes, fullwidth characters, `~user`) is caught by the independent check: the scan is
    refused and the last document is kept stale (round 8 of #288).

- **Each project's spend, this month and over the last 30 days** (J-11, OBS-93). The spend scan now reads every
  key in the OpenRouter door's ledger by its hash, one request per key. Beside the estate's month it writes
  `projects` and `unattributed` into `store/raw/spend.json` (`schema_version` 2; the existing fields are
  unchanged). `tools/spend.py project [PROJECT] [--json]` answers from that file without asking the provider
  (`monthUsd`, `last30Usd`, `keys`, `retiredKeys`, `folders`, `through`, `partial`, `unread`, `fresh`). It is the
  figure Fabric's launcher reads as a project's recent spend (contract DEC-0037).
  - A key belongs to the project named in its ledger row's `project` field. Every name of one registered project
    (its folders, its id, its name) is one bucket keyed by its registry id, so any of them answers the whole project.
  - **A retired key keeps its spend with its project** (OBS-103). The provider keeps no history of a deleted key
    (`GET /activity?api_key_hash=` and `GET /keys/{hash}` answer 404, measured 2026-10-15). So `openrouter.py
    revoke`, a completed `rotate` and `revoke --forget-pending` leave a tombstone in the ledger's `retired` list,
    kept 62 days. It holds the name, account, hash, project, day and `deleted`, and the key's spend by day: the
    activity report, then the key's own row as the last read before the DELETE.
    - The snapshot is trusted whole only when every clause holds: the month's and the week's (from Monday) days
      plus `usage_daily` equal the key's `usage_monthly` and `usage_weekly` within $0.0001, and each of the last
      three completed days falls in the month or week checked, or was reached by the report. A two-day lag on the
      Tuesday a month begins, and Monday the 1st before the report reaches yesterday, do not reconcile.
    - Otherwise every window holding a day after its last activity day, through the retirement day, reads null.
      The month window stays null until the month ends. The counter difference is not booked for the gap.
    - It is written to the ledger before the DELETE, so a failure after the DELETE keeps it for the retry. A
      rotation's tombstone is written before the successor's rename.
    - A `rotate` of a row a failure left part done finishes that rotation, then rotates afresh, so a rotate always
      replaces the value (`--leaked` and the key page say "rotated" only then). A minted successor is written to the
      row before its delivery, read by the scan, and deleted once a newer successor holds the slot.
      A stale successor still alive is snapshotted again before its DELETE, never with a snapshot kept from an
      earlier run; `issue` over a row holding one is refused until `rotate` finishes it. A predecessor
      still live is snapshotted, deleted and retired; one already gone is retired with the snapshot kept before its
      DELETE.
    - A tombstone carries its row's untracked rotation and pending predecessor, from the one rule the scan uses
      for live rows (`openrouter.untracked_through`), so a revoke never turns an honest null into a whole, lower
      figure.
    - `issue` over a row whose key is gone at the provider keeps its hash as a tombstone, with the snapshot a failed
      revoke kept. Over a key still live under another name it is refused.
    - A snapshot that cannot be read still lets the delete proceed; that key's windows are then null.
  - **Every door verb that writes the OpenRouter ledger holds one lock** (the one `issue-for-policy` held), across
    processes and threads. Two rotations at once used to drop one rotation's successor from the ledger. A second
    operation waits up to 150 s (a rotation's delivery can take a minute), and its refusal names the wait and every
    locked verb. The key server answers each request on its own thread, so a request waiting for the lock does not
    hold the page. A `retired` that is not a list is refused before any door acts.
  - `unattributed` is its own total, never guessed into a project. It holds keys whose ledger row names no project,
    and an account's spend on hashes no ledger row or tombstone holds.
  - A window that was not read whole is `null`, never a lower figure, and the key is named in `unread`. This covers
    a key the provider would not answer for or answered with malformed rows, a pending row, a tombstone without a
    usage snapshot, a hash two ledger entries hold, a rotation made before tombstones existed, and the keys not reached when the step's deadline
    (`OBSERVATORY_STEP_DEADLINE`) arrives or the provider becomes unreachable. An unreadable registry, or a ledger
    entry that is not an object, nulls every project. Both windows hold completed UTC days only.
  - The top-level `partial` stays about the estate total; `attribution_partial` covers the projects, each `degraded`
    entry carries its `scope`, and `spend.estate_month` names only the accounts not read.
  - No finding fires yet. A project's limit is enforced once a recorded spend policy is readable (J-11F).
- **The agent-network node's foundation** (OBS-107, RM-21). `observatory/engine/node/` holds what one machine
  signs to join a relay and to send a message or a blob, and how it checks what the relay delivers: RFC 8785,
  P-256 key validation, low-S ES256 and fabric-relay/0.1, ported from the contract kit and run over the contract's
  own vectors (vendored in `observatory/engine/fabric/node-vectors/`). A device-key store keeps the machine's `#sig`
  and `#kem` keys owner-only and never replaces them. Nothing runs yet: the relay session client, the estate record
  and the node service are the next slices ([NODE.md](docs/design/NODE.md)).
- **The node's relay session** (OBS-108). `node/ws.py` is a WebSocket client on the standard library and
  `node/session.py` holds one machine's connection to a relay: it signs `auth` only for the origin it dialled, sends
  signed messages, and acts on a delivered one only after checking its signature, its recipient (this machine) and
  the replay window, acknowledging it after the handler took it. `node/blobs.py` puts and fetches blobs by digest
  and refuses fetched bytes that are not the named blob.
  A relay's hello is checked before anything is signed (its origin, a 32-byte nonce), and nothing a relay sends
  ends the reconnect loop. NODE.md states the stage-1 threat model.
- **The node's estate record** (OBS-109). A node verifies a revision of the person's estate record offline — its
  digest, its chain to the parent, the passkey assertion over its challenge and the record's own rules — before it
  believes any machine the record lists, and follows the signed revocation delta and heartbeat: after 72 hours
  without a fresh heartbeat it refuses what other machines send.
- **The node's secret envelope** (OBS-113). A machine seals a secret to another machine's key-agreement key (HPKE,
  RFC 9180) and signs it; the target opens it only when the estate record lets the sender hold that slot and the
  envelope is not older than the last one it opened.
- **`project-observatory full node`** (OBS-114). `--init` creates this machine's device keys for the agent network
  and prints the device id and public keys an enrollment needs; `--show` prints them again. Private keys never leave
  their owner-only files, and a key is never replaced.
- **The estate ceremonies on the node** (OBS-133, DEC-0084). A new machine pairs with a trusted one through a one-time
  relay rendezvous: each side commits to its keys before it sees the other's, both screens show one six-digit code, and
  the new machine checks that the signed revision lists it with exactly its keys before it trusts the estate. A one-time
  enroll token is read only from stdin or an owner-only file. A revision that drops a machine or a key without revoking
  it is refused, a replaced key is revoked by its fingerprint reference, and a machine whose keys were replaced stops.
- **SSH keys on this machine** (OBS-110, PB-163). `project-observatory full ssh-keys` lists the machine's
  SSH keys (fingerprint from `ssh-keygen -l` on the `.pub`, whether it has a passphrase from the private
  file's header alone, the Host blocks and agent that use it, where GitHub accepts it for sign-in and
  signing), `create` makes an ed25519 key that never overwrites a file (modes 0600/0644, the passphrase
  typed into ssh-keygen and kept in the macOS Keychain, which also loads the key into the agent),
  `copy` puts the public line on the clipboard,
  and `send` registers it on GitHub (`gh ssh-key add`) or on a server (one line appended to
  `authorized_keys` under `sh` whatever the login shell; `--verify` logs in with no config and the
  new key alone and passes only when the server names its fingerprint), each with a receipt in the
  key movements journal. Ten findings reach the board (no passphrase, weak, readable by others and an
  open `~/.ssh` by `osprivacy`, a private key saved as `.pub`, unused, a GitHub key not found here (information only), a
  key registered nowhere visible, no public half, a missing `IdentityFile`). Observatory never advises
  deleting a GitHub key: a key GitHub accepts that it did not find is reported as information, naming
  where it looked and what it could not read, with no command. Only a private key's
  clear header is read (the cipher name and the public key), never its private section. The read-only MCP tool `observatory_ssh_keys` gives agents the same inventory and
  one key's public line. The private key is never shown or moved; tests plant one and search every
  output for it. Clouds are refused with the gap named (OBS-112); the dashboard section is OBS-111.
  [Design](docs/design/SSH-KEYS.md), scenarios OSS-62–67.

- **Survey gaps stay visible** (OBS-87): bounded checkout registration and fairer scans
  preserve older measurements with their dates. Unknown swap, failed file walks and
  incomplete registration are not reported as measured zero or fresh coverage.
  `full machine --checkouts --root PATH` records read-only checkout candidates under
  explicit roots, with bounded traversal and honest unknown/stale rows. A candidate is
  not an abandoned checkout or permission to delete it; no cleanup planner consumes it.
- **Cache coverage separates declarations from measurements** (OBS-87):
  `full machine --cache-coverage` records configured roots, available executables and
  previously measured sizes with their original dates. Unreadable sources, protected
  system areas and missing ownership or regeneration evidence stay explicitly unknown.
  The command does not scan contents, run cache tools or authorize deletion; reclaimable
  bytes and current use remain unmeasured.
- **Saved coverage is readable from the CLI and MCP**: `full machine --coverage` and
  `observatory_machine(section="coverage")` return bounded checkout/cache summaries
  without starting scans. Missing, partial, stale and invalid receipts remain explicit;
  sample rows never claim whole-machine totals or permission to clean up.
- **Open files whose path is gone are observed, not cleaned** (OBS-87). `full machine --open-unlinked` records, on
  macOS and Linux, files a process still holds open after their directory entry was removed: bounded metadata only,
  no contents and no command lines. The receipt, `full doctor` and the saved-coverage reader say what was not read;
  no row authorizes an action, and whole-machine coverage stays unknown.
- **A checkout's local state and its relation to its upstream** (OBS-87). The explicit-root checkout inventory now
  records a clean Git status apart from changes, hidden index flags and incomplete reads, and the local HEAD's
  ahead/behind counts against its locally resolved upstream. Every Git read uses an offline profile
  (`GIT_NO_LAZY_FETCH=1`), so observing a partial clone never downloads objects. A remote-tracking ref is not a new
  remote observation, and no count is cleanup authority.
- **Memory pressure, dated apart from swap** (OBS-87). On macOS the memory collector reads the platform's pressure
  level (`normal`, `warning`, `critical`) with its own timestamp and source, beside swap use and process memory. An
  absent interface, an unsupported code or a failed read stays unknown.
- **Log retention says what it did**. The maintenance sweep reports, per scope, whether its coverage was
  complete, partial or unknown, how many entries it saw, rotations completed, refusals and failures, as counts with
  fixed reasons and never paths or file contents. A receipt lost mid-sweep keeps its action counts null, because the
  effects may already have happened.
- **Each tick stage leaves a receipt** (OBS-89). `tick_progress` records which stage is running and how each ended
  (not started, stopped, failed, completed) before and after every streamed or captured step, keeping the previous
  completed report. A silent tail stage that consumed the tick is now named, and a failed receipt write never
  changes the supervised command's outcome.
### Changed

- **The `observatory-log` plugin asks for a turn's `why` once per record, with the next prompt, and
  measures the answers on what was delivered** (M-8, INB-004). The Stop hook printed the request as a
  Stop `systemMessage` after every changed turn. On one machine over fourteen days that was 3,593
  requests and 34 answers. A Stop `systemMessage` is a notice for the person and is not rendered into
  the model's input. Now:
  - **Once per record.** A session is one record revised per turn, so a session is asked once.
    `store/raw/why-asks.jsonl` (new, append-only) journals each request and each delivery. It rotates
    to `why-asks.jsonl.1` past 1 MB, under a lock, and both are read, so a record is not asked again
    when it scrolls.
  - **The record first.** The Stop hook writes the record before anything else.
  - **Not when this session wrote the rationale.** Within 2 seconds after the record is written, one
    pass over the last 4 MB of the session's transcript (named by the Stop payload) collects this
    session's writing calls:
    - Edit, Write, MultiEdit and NotebookEdit, with the file each names;
    - Bash commands that write through `>`, `>>` (also `cat > file <<EOF`), `tee` or `sed -i`, with
      the files they write.

    No request is made in two cases:
    - one of those calls targets a decision register (a `decisions.md`, or a `.md`, `.markdown` or
      `.txt` file under `adr/`, outside vendored trees and submodules, not deleted, with at least one
      non-blank added line, whitespace ignored), and its text carries the entry's own heading (the
      shallowest heading the work added, never a repeated `### Consequences`), or, with no heading,
      more than half of its added lines; that heading must appear in the register exactly once, as a
      heading line outside fenced code;
    - one of those calls wrote `.task-pipeline/run.md` (or ran `stage_checkpoint.py`), and a
      `decision:` or `stage:` line among the ledger's newest 64 KB and 200 such lines, read even when
      git ignores it, has its text in what those calls wrote.

    The pass skips calls whose result was an error and subagent (`isSidechain`) lines, and it does
    not scan a Bash command longer than 65,536 characters. `sed -i` is read by splitting a command on `;`, `|`,
    `&` and newlines, so the scan is linear in the command's length.

    A second revision of the same record then links the file, and its `why` stays empty. The link
    stores the repository's id, the register's path, the entry heading as `anchor`, the exact raw
    heading line at full length, and the length and SHA-256 of the whole register file; no entry
    boundary is guessed. A heading over 120 characters is never linked. A later revision carries the
    link only while it holds by bytes: this hook wrote it, it names the current repository, the raw
    line occurs exactly once as a whole line, and the file is byte-identical or its linked bytes are
    unchanged and what follows starts, at its first non-blank line, with a column-0 heading no
    deeper than the entry's own (accepted only when the linked file ended in a newline outside any
    code fence and HTML comment). Appending a later entry keeps the link. So the record stays explained, with no request and no
    extra revision, after the register is committed and pushed and after the write scrolls out of the
    transcript. Any other change to the file drops the link: an edit to any entry, a paragraph or a
    deeper heading appended, a change of line endings, or an edit below a `# comment` line inside a
    code block or HTML comment. So does a move to another repository. A dropped link is attributed
    afresh only from the session's writes after it was made (the mark is carried in every later
    revision), and asked about when there are none. A turn
    that touches a register is attributed again, and a newer decision replaces the anchor.

    Every read of a register or the run ledger, fresh or carried, passes one gate: inside the
    checkout, not a symbolic link and not under one, opened once without following a link or
    blocking, a regular file with one hard link, read up to its limit plus one byte. A fresh anchor
    is chosen by a linear Markdown heading scan (newline split only, fences and multi-line comments
    skipped, `## Use C#` keeps its `#`), and an added block whose heading syntax the scan rejects is
    asked about, never matched line by line. The attribution's `git diff` gets only the time left in
    the 2 seconds.

    The turn is asked about in every other case:
    - another session's edit in the same checkout;
    - a file the session only read or grepped;
    - a pull-request body that shares a heading;
    - a write by a tool the pass does not know (`cp`, a script, `perl -i`);
    - `sed -i` behind a prefix (`sudo`, `xargs`, `env`), `gsed`, or a sed script containing `|`
      or `;`;
    - a relative path written after a `cd` in the same command;
    - a path whose letter case differs from the file's on a case-insensitive volume;
    - a subagent's edit, a failed call, or a command longer than 65,536 characters;
    - no transcript;
    - a pass that runs out of its 2 seconds.
  - **Where the model reads it.** A new `UserPromptSubmit` hook, `hooks/prompt-why.py`, hands the
    pending request to the model as `additionalContext` with the next prompt, once, and records the
    delivery. The context is capped at 4,000 characters, and a request that does not fit waits for the
    following prompt without being marked delivered. It prints nothing when nothing is pending, never
    writes through a symbolic link, and reaches no file for a session id that is not a plain name.
    - The request waits in `$XDG_STATE_HOME/project-observatory/ask-why/<session>.json` (0600). An
      undelivered one older than seven days is pruned by either hook.
    - If that file cannot be written, the request falls back to the Stop notice. An older engine whose
      recorder does not decide whether to ask also gets the Stop notice, never an injected prompt. A
      fault to record is still a Stop notice: the recorder's output to the hook now carries `fault`,
      which only its receipt carried before.
  - **Answering keeps the session recorded.** The answer the request asks for,
    `observatory_record(owner, memory_id, expected_revision, statement, why)`, is written as a note with
    no session id. The next turn revises the record from its latest revision, whoever wrote it, and
    keeps the `why`; before, it revised from its own last revision and every later turn of the session
    failed with a revision conflict. A latest revision that names another project is not revised: the
    turn starts a new record that `continues` it.
  - **One new record, not one per turn.** The Stop hook picks the session's record by the newest row
    the session wrote, not by the highest revision number. A record it starts after a promotion, a
    rejection or a takeover by another project begins at revision 1, so it lost to the old record, and
    every later changed turn started another record and asked again.
  - **Measured.** `store/raw/record-turn.json` gains `whyAsks` (`asked`, `delivered`, `pending`,
    `answered`, `rate`, `window`), with rate = answered ÷ delivered. A request that never reached the
    model, such as a headless single-prompt session, is pending and never unanswered.
    - `why_asks.py` prints the same.
    - `why_asks.py reached <transcript> <memory id>` says whether a request reached the model in a
      session transcript.
    - The new finding `observatory.why_unanswered` fires below a 10 % rate over seven days with at
      least 20 delivered requests.

  `tests/test_skill.py` T8 now expects the `UserPromptSubmit` hook it used to forbid, and runs it to
  show that it prints nothing with no request pending. New suites: `tests/test_ask_why.py` and
  `tests/test_ask_why_reach.py` (a synthetic transcript fixture).

### Fixed

- **A worktree of a repository with no remote is no longer a second project.** The filesystem scan records the
  checkout a worktree's `.git` file points at (`worktree_main` in `store/raw/local.json`), and the merge folds a
  remoteless worktree into its main checkout's project, named in that project's membership rules. With a remote the
  repository pass already did this; a worktree whose main checkout was not scanned stays visible as its own project.
  Found by the automatic project registration run's isolated cross-product scenario
  (org-index `docs/runs/2026-10-17-automatic-project-registration/`).

- **Cleanup preserves unverified local state** (OBS-84, OBS-86): ignored files, nested
  repositories, hidden index flags and unresolved worktree state prevent automatic
  removal. A manual dirty-worktree removal verifies its archive against current tracked,
  staged and untracked content. Branch cleanup binds archive/proof and deletion to the
  same commit and refuses symbolic refs. Disk-guard actions recheck target identity and
  safety evidence before removal; the legacy unverified build-artifact action is refused.
  Log rotation also refuses symbolic links, linked directories and hardlinks before
  rotating or changing permissions. External writers can still race the final filesystem
  check: use an idle workspace. Copy/truncate can still lose a concurrent append.
  Incomplete or unreadable process listings cannot declare an omitted target free;
  clean and archived dirty worktrees repeat a bounded holder check immediately
  before removal. A missing worktree's registration goes only when its directory is
  proven absent (an unplugged drive, a renamed parent folder or a permission error is
  unknown and prunes nothing) and after its HEAD is kept by its branch or an
  `archive/wt-<sha8>` branch; only that registration's `.git/worktrees/<name>` entry is
  moved aside to `.git/worktrees-retired/`, never a working directory and never a
  repository-wide `git worktree prune`, which could make a detached worktree's commits
  unreachable. Failed removal reports unknown effects instead of claiming success. A
  branch whose upstream is local (`branch.X.remote` set to `.`) is no longer deleted as
  pushed. Log rotation follows links above the workspace (macOS `/tmp`, `/var`, a linked
  home) and still refuses them inside it, a dangling link or a `..` included, and a log
  folder that does not exist yet is reported absent, not unknown.
- **0.25.0 reclaims less disk than 0.24.5.** With holder coverage unknown (no complete
  process inventory on any platform), the disk guard and worktree cleanup now remove
  nothing that needs a holder check: build output, caches tied to a running tool and
  worktrees are kept and reported, where 0.24.5 removed them. This is deliberate: a
  path a process may still hold is not deleted on a guess.
- **A failed check retains its peers' receipts** (OBS-85): setup, execution and report-write
  failures produce error evidence without losing completed suite results. Unknown
  completion fails the run instead of becoming a passing or empty summary.
- **Findings follow the measured cause**: collector deadlines distinguish work not started
  from interrupted work; partial leak scans retain sightings; memory and swap findings use
  their own measurement freshness and preserve unreadable-source degradation.
- **Engine-update snapshots stay visible**: backup status and verified migration now
  include local `before-update` snapshots. Recognized snapshots share one kind list;
  damaged copies remain reported and retained, while foreign kinds remain untouched.
- **Cleanup child receipts are validated before use**: failed commands and malformed
  summaries remain failures. Valid summaries retain only known structured fields;
  exception details, child output and extra fields do not enter persisted diagnostics.
- **Scoped Heroku refreshes preserve the rest of the survey** (OBS-80): `scan_heroku --only`
  updates only successfully measured apps, retains unrequested rows and the global survey
  timestamp, and records per-scope results. Missing apps, malformed responses and invalid
  costs preserve old measurements with degradation. Provider-only refreshes retain local
  checkout information and its unknown state until a full scan measures it. Full and scoped
  collector invocations share a writer lock; no provider state is changed by the collector.
- **An offline check passes only on executed tests** (OBS-85). A unittest suite that defined cases but never ran
  them exited 0 and passed with zero tests. Suites now declare their reporting protocol: unittest suites need a
  complete summary with at least one executed case, the legacy PASS/FAIL scripts keep theirs, and an earlier failure
  is never hidden by a later clean summary.
- **Cleanup refuses when a process might still hold the path** (OBS-86). On Windows there is no holder inventory and
  a successful rename no longer counts as proof that nothing holds a folder (a handle opened with shared delete
  allows the rename), so path removal there is refused; older rename-mode state refuses too. On POSIX, `ps` renders
  arguments with spaces in place of their boundaries, so a missing match is no proof of absence: positive matches
  still hold a path, but an absent one no longer admits deletion or a cache tool's busy check. Preliminary admission
  no longer captures the process table it cannot use; explicit holder captures are bounded (2 MiB per child, a
  shared deadline, only the capture's own process group stopped), and the old synchronous `/proc` fallback is retired.
  A process starting after the final check remains a race.

### Documentation

- **The README's capability table names records and lessons** (N-4, USR-009). A new row, "Can agents keep what
  they learned?", names `observatory_record`, `observatory_learn`, `observatory_lesson`, `observatory_search` and
  `observatory_recall`, says every write is a proposal, and links [Install](README.md#install) and the
  [MCP section](README.md#mcp).

## 0.24.5 — 2026-10-14

The leak scan reads every agent's transcripts (Codex, Hermes and opencode beside Claude Code) and a scan its
deadline cuts short still reports what it saw. Cleanup never removes local work it cannot restore, and the disk
guard registers worktrees cheaply before its expensive passes. Findings say when the tick, the harness scan or the
guard could not measure, and RAM and swap refresh on their own. Lessons become their own record kind; OpenRouter
spend per issued key; a DigitalOcean database door. The companion plugin `observatory-log` moves to 0.24.5: its
`handling-secrets` skill names the DigitalOcean door, the provider tools that return a credential by design, and
the header door's `--server-url`.

### Added

- **The leak scan reads every agent's transcripts, not only Claude's** (I-5, PB-032 remainder).
  Before this, a value echoed into a Codex rollout was neither a sighting nor named as unread.
  `paths.transcript_homes()` names each home, with the tool's own source as evidence:
  - **Claude Code:** the `sessions` source, and `history.jsonl` in `$CLAUDE_CONFIG_DIR`/`~/.claude`.
  - **Codex:** in `$CODEX_HOME`/`~/.codex`, `sessions/` and `archived_sessions/` (`rollout-*.jsonl`,
    and the compressed `.jsonl.zst`, read with Python 3.14's zstd) and `history.jsonl`; in
    `$CODEX_SQLITE_HOME`, the `state_*`, `logs_*`, `thread_history_*`, `memories*`, `queue_*`,
    `goals_*` and `agent_message_board_*` `.sqlite` databases.
  - **Hermes:** `$HERMES_HOME`, or `~/.hermes` with `$HERMES_DATA_DIR_SUFFIX`, and its profiles:
    `state.db` and `sessions/*.json|*.jsonl`.
  - **opencode:** in `$XDG_DATA_HOME/opencode`, the `opencode*.db` databases or `$OPENCODE_DB`, and the
    legacy `storage/**/*.json`.

  An absent home is not a degradation. These are named in `not_scanned` with the reason: a home that
  exists but will not read, a profile that is a symbolic link, a database that will not open, and a
  `.zst` this Python cannot decode. `coverage.transcript_homes` says which homes were `absent`,
  `scanned`, `partial` (cut by the step's deadline) or `unreadable`. Sightings name the file or the
  `table.column`, never the value.
- **A database's mark hides fewer rows from the leak scan** (I-5). Each table's signature is kept
  beside its mark: the inode, highest rowid, count, rowid sum and sum of squares (two primes),
  highest update value, and digests of the newest row and of the rows at the update maximum (one
  digest for the whole set past 64 rows). These are now read again:
  - rows in a store replaced by another file;
  - rows whose set of rowids under the mark changed (some deleted, or one inserted under the mark
    with a new rowid);
  - the deleted newest row's rowid when it is reused;
  - a row rewritten in place whose update column moved, or stayed at its maximum with a new digest
    (the column preferred finest-first: `updated_at_ms` before `updated_at`). A bulk update is
    reported once, not every tick.

  The weekly complete pass is still the only one to see:
  - a row replaced under its own rowid below the newest row, in a table with no update column;
  - an edit that moves no column (Codex `UPDATE threads SET title`);
  - an edit below a future-dated maximum;
  - an update column holding mixed types;
  - a change of rowids whose count and both sums collide (impractical, not impossible);
  - Codex `thread_items`, which is upserted in place; its rollouts are read incrementally.

  Rows are read 500 at a time, each page its own statement. The signature's aggregates each scan
  the whole table in one statement, so on a rollback-journal database a writer can wait for one
  such scan. Codex, opencode and Hermes run their stores in WAL mode by default, where a reader
  does not block a writer.
- **A damaged table is a hole, never a clean table** (I-5). Any error from a table used to be
  taken for a WITHOUT ROWID table, and that table's read then stopped without a word: a damaged
  page left the store `scanned` with nothing degraded. Only a table declared `WITHOUT ROWID` is
  read whole now; SQLite itself says which tables those are (`pragma_table_list`), so a comment in
  the CREATE statement cannot. Any other error names the store in `not_scanned` as read only in
  part, with each table and the error's type, never SQLite's message, which can quote a cell.
  Its other tables are still read, and its marks do not move.

  Other inputs that used to fail are now handled:
  - Text that is not UTF-8 is read as its bytes.
  - A table name that is not UTF-8 is a hole. A column name that is not UTF-8 is a hole of its
    own, and the rest of its table is still read.
  - VIRTUAL generated columns are left out of every read, so one that fails no longer blinds its
    table. STORED generated columns are read, because their values are on disk.
  - A column named `rowid` no longer stands in for the real rowid. Rows are paged on whichever of
    `rowid`, `_rowid_` and `oid` the table leaves free; one that takes all three is read whole.
  - A store that fails in any other way is "stopped while reading (<Type>)". Its sightings
    counted so far stand, its `rows_read` counts the rows it read, and the rest of the scan
    and its report go on.
  - A mark in the state file that is not a rowid is treated as absent, and its table is read
    from the start.
  - `VARCHAR(n)` and `NCHAR(n)` columns are read, by SQLite's TEXT affinity rule.

  Every SQLite store error is now reported by its type only, including "would not open" and a
  malformed schema, whose message quotes the stored CREATE statement. Before, those two copied
  SQLite's whole message into the report, the `secret.leak_scan_blind` finding and stdout.

  Not read: rowids of 0 or below, and text kept in a column without text affinity. A
  `WITHOUT ROWID` table is read whole every pass.
- **Upgrade cost, and two changed findings.** The first passes after the upgrade read every Codex
  rollout in the 14-day window, both prompt histories and every transcript database whole, and
  re-read the tracked tree, the built page and the two engine stores once. The first tick or two
  may therefore raise `secret.leak_scan_partial`. A store that will not open is now an unreadable
  source, which raises `secret.leak_scan_blind` from `info` to `warning`.

- **What one issued OpenRouter key cost, by day and model** (INB-019): `openrouter.py spend NAME [--date
  YYYY-MM-DD] [--json]` reads the provider's activity report through the management key that issued the key,
  filtered by that key's hash — the last 30 completed UTC days, or one day. The hash goes to the provider only and
  never to the output. A refused or unreadable report is said as such and is never printed as $0.
  `read_activity()` is its own function, so a project's spend can call it. An agent whose provider call ended
  without a request id can reconcile it from this record.
- **A lesson is its own record kind, for every project** (`observatory_lesson`): a retro entry's class —
  `classKey`, `title`, `trigger`, `check`, `nouns` — with its origin, the project's pseudonymous key
  (`p-` and 12 hex digits of its root commit, never a `project:<slug>` id) and the evidence commit (kept as
  `commit:<12 hex>`). The arguments are what task-pipeline's `lessons.py emit-observatory --tool
  observatory_lesson` prints (where the engine lacks the tool, the emitter writes `lesson:` notes through
  `observatory_record`); `tests/lesson_emitter_fixture.json` holds that shape and the published schema is
  checked against it. One record per (classKey, project): a repeat revises it or writes nothing. A
  path, a URL, an e-mail address, a line break or a `classKey` the redactor would change is refused before any
  write (`invalid-lesson`, every reason listed); credential shapes and known values are redacted. A promoted
  lesson that cites a new commit, or whose evidence stops resolving in two known projects, is archived and
  replaced by a new proposal (`outcome: replaced`), which is promoted again only once its commit resolves.
  Written `proposed`, confidence 0.5, local stdio only. The tool publishes its outputSchema.
- **`observatory_search` and `observatory_recall` take `kinds`**, applied inside the candidate query before the
  300-match window, and every hit names its `kind`; a lesson hit carries its fields as `lesson`. Across projects
  the search abstained with the window full of per-session turn records (measured: `count 0, abstain: true`);
  with `kinds: ["lesson"]` a lesson among 400 such rows is found.
- **Lessons are corroborated by a second project**: `tools/corroborate.py` promotes a proposed lesson to
  `observed` as `agent:lesson-corroborator` when another, distinct project records the same `classKey` and both
  commits resolve — each asked of git in the registered clone whose root commit gives its project key. A key no
  registered clone carries is no witness: its lesson stays proposed, listed as "commit not resolvable here". The
  same project twice is one witness; `supported` stays the operator's. `corroboration.json` lists lessons apart
  from refusals (`lessons`: promoted, waiting, unresolvable, contradicted), and `counts.proposed` no longer
  counts them.
- **Lessons are kept 365 days while proposed, and for good once observed** (`retention.json` →
  `ledger.kinds.lesson`); any kind can carry its own horizons there, read through `retention.horizon_for` by
  the review queue's countdown and prose, the `ledger.review_expiring` finding and the health page's digest,
  so none announces a proposed lesson's erasure at the general 90 days.
- **`tools/migrate_lessons.py`** converts lessons written earlier as `observatory_record` notes (`lesson:`
  statements) into lesson records, as a new revision of the same record; `--dry-run` is the default.

Lessons are agents' work memory and stay in Observatory; an export of project-grade lessons to Fabric project
memory is a later seam ([agent memory → Lessons](docs/design/AGENT-MEMORY.md#lessons)).
- **A DigitalOcean door: database credentials to the vault, and a rotation that moves every consumer you name** (PLN-142):
  `tools/digitalocean.py` over the operator's `doctl` context. `db list` names clusters and which are open to
  the internet; `db consumers CLUSTER` names the App Platform apps that bind one and the password keys they keep
  in their own spec, each marked `url` or `password`; `db credential CLUSTER USER --db NAME --to
  vault:<project>/<env>/<NAME>` delivers a connection URL on stdin ("unchanged" when the slot holds it);
  `db rotate` moves every consumer you name (`db consumers` lists the App Platform ones; services started with
  `use_secret.py serve` are named for restart): it prints the doctl context and team, asks the vault for the
  slot and reads every spec before the reset, resets once, writes the URL or the password into each
  `--spec-env APP:KEY` literal as the key expects, redeploys each `--app`, retries read calls only and
  waits for exact ACTIVE. Mutations are attempted once; uncertain outcomes remain unknown in
  owner-only receipts and require reconciliation before retry. Nothing
  prints a value. The handling-secrets skill names the provider MCP tools that return a credential by design.
  Its suite runs in the portable gate.
  - Three more refusals before the reset (exit 1, nothing reset): a `--db` that is not one of the cluster's
    databases (a cluster the provider lists no databases for, and a redis/valkey cluster, are refused with their
    own reason, and only identifier-shaped database names are printed); a `--spec-env` key that is a `${…}`
    binding at every level (name the app with `--app`); and a binding beside a literal of the same key that
    cannot be proven to hold the selected user's credential. A binding is never rewritten.
  - `db credential` checks `--db` the same way before it reads the password, so the recovery that rotate's
    exit 4 and exit 5 point to cannot write a URL to a database that does not exist into the vault.
    It also checks the cluster's engine, so a mongodb, kafka or opensearch cluster is refused by name: the door
    builds pg, mysql, redis and valkey URLs only.
  - `db consumers` lists a key whose `_`-separated name has a database word (`DATABASE`, `DB`, `PG`,
    `POSTGRES`, `POSTGRESQL`, `MYSQL`, `REDIS`, `VALKEY`, `MONGO`, `MONGODB`) beside `URL`, `URI` or `DSN`
    anywhere in it, encrypted or not (`APP_DATABASE_DSN`, `PRIMARY_DB_URI`); `SENTRY_DSN`, `OTEL_…_URI`,
    `DBT_URI` and `DBX_DSN` stay out.

- **The header door serves loopback servers and clients that export no URL** (PB-139): `vault.py bind
  --header-for` accepts plain http to a loopback literal with its port (`http://127.0.0.1:<port>`,
  `http://[::1]:<port>`); `localhost` and http to any other host stay refused. `use_secret.py header` gains
  `--server-url URL` for Codex's `http_headers_helper`, used only when `CLAUDE_CODE_MCP_SERVER_URL` is unset;
  two URLs that name different servers are refused (`url-conflict`), and the audit row names the `caller`.
- **A manifest never pins a tag its release lacks** (ENG-018): 0.24.0 shipped `fabric-agent.json` with the
  `grant.check` schemas at `/v0.24.1/`, a tag that did not exist until the next release, so a host admitting
  0.24.0 read 404s. `observatory/engine/tools/check_manifest_urls.py` refuses a pinned tag newer than the release
  and any moving ref (a commit sha is admitted); the release workflow's `manifest` job runs it, reads every URL
  once on a publishing run, and `publish` waits for it.
- **The board lint and the OBS id register**: `tools/check_backlog.py` (run by the root tests) refuses what is
  false on the engine board once merged — duplicate or unreserved ids, statuses outside the vocabulary, "in this
  PR"/"to follow"/"once … restarts", `done` with a remainder, a `done in X.Y.Z` with no such release here, rows
  wider than the header, and private names in the board and this file (with the maintainer's local denylist).
  `.claude/agent-sync.json` declares the `OBS` register, so a branch reserves its row's id before writing it.

- **RAM, swap and process memory refresh on their own** (OBS-87, #257): `project-observatory full machine
  --memory-only` samples RAM, swap and per-process RSS (by executable name, never a command line) into
  `machine-memory.json` without sizing the disk, and the disk guard takes this sample before its expensive
  phases. Each source carries its own time: the Machine page, the CLI and MCP say how old each is, an unknown
  swap stays unknown rather than zero, and a failed sample never re-dates an older one. RSS totals are sums that
  can count shared pages more than once.
- **Worktrees are registered before cleanup classifies them** (OBS-87, #255): `collectors/scan_git_hygiene.py
  OUT --registrations-only` lists each repository's worktrees with `git worktree list --porcelain` alone, within
  its budget, into `store/raw/worktree-registration.json`; a repository not listed in this pass keeps its old
  list, marked `stale`. The disk guard refreshes registrations first, starts each pass where the last one stopped
  (a persisted cursor), bounds each root and keeps each phase's holder evidence. A registration never
  authorizes a removal.

### Changed

- **The engine board is the organization's canonical format** (L-1): `docs/backlog.md` holds its table in one
  `## Board` section that org-index's register check reads as an allowlisted format, and `tools/check_backlog.py`
  holds the board to that format, reading the raw bytes as that check does: a CR anywhere, a second or missing
  `## Board` or one hidden by an HTML block, comment or fence above it, a header or separator not exactly the
  canonical one, text after the table, an OBS row outside the section, a backslash (an escaped `|`), `<` or an
  invisible character in a row, two `parent:` tokens in a Source. Above the board, a line starting with `<` opens
  an HTML block until the next blank line, so `<3 text` right above `## Board` hides it; inline tags mid-line, and a
  `<`-line that is no tag closed by a blank line, pass. It is stricter than that check in these places, each named
  in its docstring: any code fence above the board, even closed, and any `<!--`, even mid-line and closed; every
  Unicode control, format and separator character and every space but U+0020, not a fixed list; the Hangul
  fillers (U+115F, U+1160, U+3164, U+FFA0), U+034F, the Khmer inherent vowels, the Mongolian and Unicode
  variation selectors, and unassigned default-ignorable code points (U+2065, U+FFF0–FFF8, U+E0000, …); ids in
  ASCII digits, `OBS-001` and `OBS-0` refused, `OBS-01` beside `OBS-1` one id written twice; a `parent:` PB id
  in ASCII digits; `superseded by OBS-n` naming a row spelled exactly so. A Status cell is exactly one of `done`, `done in
  X.Y.Z`, `done in X.Y.Z (#n)`, `done — merged (#n)`, `done — merged (#n), unreleased`, `cancelled` or
  `superseded by OBS-n`, or `open`/`blocked` with a note; a closed status takes no note, so 42 rows moved theirs to
  the Item column ("Outcome:"), `closed` became `superseded by OBS-17`, and OBS-63 reads `done in 0.24.4`. Rows
  name their package as `parent: PB-nnn`. OBS-56 is done for the estate total, its per-project remainder is
  OBS-93; OBS-74 credits 0.24.0 with the finding only; OBS-75 is done (the old key was revoked on 2026-10-12);
  OBS-62 reads `done in 0.24.0 (#206)`, the release its code reached; OBS-92 records the loopback header door.

- **A spend policy's keys share its limit** (ENG-002): `openrouter.py issue-for-policy` gave every `project-key`
  source the whole project limit, so a $40 project with two keys could spend $80. The ceilings of every key the plan
  touches now sum to at most `limitUsd`: an equal split rounded down to the cent by default, `--split REF=USD,…` for
  explicit amounts (refused over the limit, or with a slot left out), and `--limit` now one ceiling **per key**,
  refused when N × limit exceeds the project limit. A held slot is read at the provider by hash: a disabled key is
  `held-disabled` and a deleted one `held-missing`, never `in-place`, and the command exits 1 naming the remedy.
  Ceilings that go down move before those that go up. A failure halfway exits 1 with `partial`, listing what was
  already issued or moved; a re-run does only what is missing. The plan prints the ceilings' total beside the limit.
  A provider row with no ceiling counts as unbounded and is brought under the limit first; a provider answer that
  cannot be read is `held-unread` (exit 1); the dry run now reads the provider too. Keys issued earlier keep their
  whole-limit ceilings until `issue-for-policy` runs again — `--dry-run` shows the moves it would make — and a key
  lowered below what it has already spent this period stops spending until the period resets. A provider row's
  reset period is read from the provider alone (none there differs from a monthly policy, so the ceiling is moved),
  and `--split` naming a slot twice — in one spelling or two — is refused. **Upgrade note:** the meter's L0 per key
  is configured, not read from the provider: after `issue-for-policy` moves a ceiling, set `keyCeilingsUsd[ref]` in
  `config/meter.json` to the same amount — the plan prints the values it implies. The dry run reads the provider, so
  without a stashed admin for a slot's account its held slots read `held-unread` and it exits 1. The total holds at
  every moment, not only at the end: every key the ledger holds under `vault:<project>/` counts, including one the
  policy no longer names (a removed source, a second key behind one slot), which is **not moved** — it is reported
  `not-in-policy` with `revoke NAME` as the remedy and the run exits 1; a key that cannot be read counts at its
  ledger ceiling (unbounded with none), only a provider 404 counts as gone, and a key renamed in the provider's
  dashboard is still found by its hash (this applies to `limit`, `rotate` and `revoke` too). Lowerings run first;
  raises and new keys run only when the project's total after the run fits the limit, otherwise they are `blocked`
  and the run exits 1. One run at a time: a lock beside the ledger is held from the plan to the last write, a second
  run waits up to 30 s and then refuses. Every amount is rounded down to whole cents and compared with the exact
  limit; ceilings within half a cent of the target are not moved again; a limit past $1,000,000,000 is refused; two
  slots whose key names differ only in case are refused; the issuing account of a new key is resolved before
  anything moves, in a dry run too; a minted key whose delivery times out is deleted, never left live and untracked.
  Totals are kept in whole cents (planned ceilings exactly, provider ceilings rounded up, unbounded apart), so $0.30
  over three keys is three $0.10 keys rather than a false block. `issue` writes a **pending** ledger row (name,
  destination, ceiling) before it mints and records the key's hash and provider label the moment the provider
  answers: a run killed between the mint and its record, or a cleanup delete that failed (the row is then kept as an
  orphan), leaves a row that holds its slot — the next run mints nothing there, reads `held-pending` and names
  `revoke` (or, with no hash, `claim` or a delete at the provider) — so an account listing past `PAGES_MAX` no
  longer mints a second key. Whenever the total a run leaves is past the limit it prints `OVER LIMIT by $X now ($A
  against the $L limit); it ends after: revoke A, B` (`OVER LIMIT: unbounded now` when a key has no ceiling) and exits 1, a total past the limit only within
  the half-cent tolerance included. `revoke` refuses a key a consumer reads, found by its hash in the scan or by the
  label recorded at issue, whatever the key is now called at the provider. The one window in which a project's
  keys can sum past its limit is `rotate`'s, while the successor and the predecessor are both live.
  **Until the J-3 change, a `rotate` that fails is not counted:** a provider error after the mint, a timeout, a kill,
  a vault put that fails or a predecessor delete that fails can leave the successor `NAME-next` live and untracked,
  or the predecessor live and uncounted (the row reads `rotation_status: successor-delivered-predecessor-pending`
  with its `predecessor_hash`), and `issue-for-policy` does not see either; delete at the provider whichever of the
  two the slot does not hold, then run `issue-for-policy`. A mint answered with a 5xx or a timeout keeps its pending
  row (only a 4xx proves nothing was minted), so an account listing past `PAGES_MAX` never mints the slot twice;
  `revoke NAME --forget-pending --reason "…"` clears a pending row with no hash there once its key is gone, after
  journaling the reason as an attestation (no journal, no clearing). `revoke` also refuses a key whose label a
  destination holds in the scan (`destinations`, the only trace of a key past the listing), and a ledger write that
  fails inside it exits 1 instead of a traceback. A provider ceiling $0.0001 above the plan counts as the next cent.
- **The agent's deadline reaches the model call** (ENG-003): `agent` started a project's call while 150 s were
  left, but `providers.complete()` walks a chain of models, each attempt allowed 120 s, so three Retryable models
  took 360 s and the watchdog killed a step that had paid for calls. `complete(…, deadline=)` now gets the step's
  deadline: an attempt starts only while 30 s are left after a 20 s reserve for the report, its timeout is cut to
  what is left, and otherwise it raises `providers.OutOfTime` before sending anything; the catalogue fetch and the
  key-usage read inside are cut the same way. An attempt sent with a cut timeout that then times out also ends the
  call as `OutOfTime`, without marking the model unhealthy — that attempt was sent and may have been billed. The
  agent treats `OutOfTime` like its pre-call halt — deltas left, `halted_by` names the deadline and says when the cut
  attempt may have been billed — and `store/raw/agent.json` gains `attempts_refused_by_deadline` (never sent) and
  `attempts_sent_and_cut_by_deadline` (sent, possibly billed). A model catalogue fetch the deadline cut, with
  nothing cached, is `OutOfTime` too rather than "every model unavailable"; a connection that dies mid-body
  (`http.client.HTTPException`) is Retryable instead of crashing the run. The pre-call reserve is derived (50 s),
  not a constant. Without a deadline nothing changes.
- **The tick says how each collector ended** (ENG-005, ENG-007): `store/tick-rotation.json` is schema 2 — per
  collector `started_at` (stamped before the step; schema 1 kept the time after it), `finished_at`, `outcome`
  (`ran`, `failed`, `stopped`, `not_started`), `not_started_streak` and `last_ran_at`; a collector never started
  keeps `started_at: null`. The start is saved before the step runs, so a tick killed inside a step still moves it
  back (`outcome` and `finished_at` null mean started and then killed). A schema-1 record is still read — its times
  seed `started_at` and `last_ran_at`, which may therefore name a run that failed — and rewritten as schema 2; a bad
  or non-finite row is coerced or skipped, not the whole record; a newer engine's schema is left untouched. Every
  `tick.json` `failed_steps` row gains `outcome` beside `exit` — the bails at merge, emit and validate included,
  which now keep their own exit instead of a literal 1 — so a step the watchdog did not start is told from one it
  stopped at its limit (both exit 124). The distinction is in `tick.json` now; the board's `tick.step_failed`
  titles use it with K-2's change. The tick log names a collector not started for consecutive ticks at plan time.
  `rotation_order` no longer loops on a cycle in `READS_FROM`: it names the cycle in the log and runs the planned
  order, and `tests/test_pipeline.py` asserts `READS_FROM` is acyclic.
- **The demo's findings speak the page's language** (OBS-64): `tools/demo_estate.py` wrote its five findings as
  English sentences, so a Russian screenshot mixed two languages. Each title is now a rule's own message id with its
  arguments, built by `finding_types.titled` like a real finding, so the Russian page renders them from the same
  catalog; the detail and the action stay in English, as a real rule writes them.
- **The disk table says what a removal frees on APFS** (OBS-66): `du` counts an APFS clone in full — 19
  abandoned browser code-sign clones read as 13 GB and removing them freed nothing. On macOS a place of 512 MB
  or more is walked a second time, inside the same budget, for each file's private size
  (`getattrlist(ATTR_CMNEXT_PRIVATESIZE)`); the row gains `frees_at_least_gb` and `clone_shared_gb`, and where
  clones hold a tenth of the place (and 100 MB) the Machine page and `machine.disk_low` name the floor. The walk
  costs about three times `du`'s time (6.3 GB of caches: 7.6 s against 24.7 s under load); one that does not
  finish keeps the last walk's figures, dated, and says so (ENG-006 below).

- **The Russian copy speaks one vocabulary** (#241): 392 dashboard catalog entries and 52 Mac and desktop app
  strings are rewritten without calques or English left mid-sentence, with one word for one thing and the neutral
  address of `docs/brand/locales/ru.md`. Keys, placeholders, commands, paths and slot names are unchanged.

### Fixed

- **A store path holding `?`, `#` or `%` is read, read-only, as the path it is** (I-5). Every
  `sqlite3.connect(f"file:{path}?mode=…", uri=True)` in the engine let SQLite cut the path at a
  `?` or `#`. That affected the leak scan and also Observatory's own store in the dashboard,
  findings, sessions, MCP, backup, ledger export, review and companion scrub. The `mode=ro`
  after the cut was lost: the store opened read-write and created an empty file where the cut
  fell, while the real store was never read, so the leak scan reported it clean. A `%XX` was
  decoded into another path.

  All of them now go through `paths.sqlite_uri`. It percent-encodes the absolute path without
  looking it up, and so never raises: `Path.resolve()` raised RuntimeError on a symlink loop on
  Python 3.11 and 3.12. `..` is kept, so `C/link/../store.db` names the file the operating
  system and the plain-path writer reach through `link`. Only a trailing slash and `.` parts are
  dropped. The modules that already built
  their own URI with `as_uri()` (`store/compatibility.py`, `workspace.py`,
  `workspace_upgrade.py`, `service_events.py`) are unchanged, and the store's writer
  (`store/db.py`) opens by plain path. An AST test refuses any `connect(…, uri=True)` in the
  engine whose URI is not built by `sqlite_uri`, `readonly_uri` or `<path>.as_uri() + …`,
  directly or through a name bound only to those in the same function. `trace_opens` decodes
  such URIs back into their paths.
- **A file rewritten in place is read again whole by the leak scan** (I-5): only a `.jsonl` is
  append-only. A rewritten file (a session JSON, the built page, a tracked file) used to be resumed from
  its old offset, so a value written before the old end was never read; it is now re-read whenever its
  size or modification time changes.
- **A leak scan cut short by its deadline still reports what it saw** (ENG-001): any `degraded` entry in
  `leak-scan.json` used to turn the whole board into "the leak scan had nothing to look for", so a pass that
  had already found a credential before its time ran out reported nothing. Each entry now carries a `code`
  (`no-values`, `deadline-targets`, `deadline-store`, `source-unreadable`), and `coverage` gains
  `targets_read` and `stores_unread` (`~`-relative). Only a scan that knew no values is
  `secret.leak_scan_unmeasured`; every other one keeps `secret.seen_outside_its_home`, and a cut pass adds the
  new warning `secret.leak_scan_partial` ("the leak scan read X of N sources; stores unread: K"), which stays on
  the board and is not pushed by `notify_findings` (the sightings beside it are critical and are). `partial` is
  now true only for a deadline cut; each entry also carries `source` (`leak-scan:<code>`) and `reason`, so
  `/health` names it. A sighting is reported even beside "nothing to look for". A document written by an older
  scanner reads as before, and a cut one says the scanner recorded no coverage instead of inventing a count.
  **After installing, the first full pass may raise real `secret.seen_outside_its_home` criticals** that a cut
  pass used to hide. The first passes after install are forced full (the known values change with INB-002, and
  changed values reset the offsets), so they stay `partial` for several ticks until the scan has read every
  target once. An unreadable source (`source-unreadable`) holds `/health` as degraded: unread is not clean —
  a session folder or a vault folder (root, project, env) that will not list, a sessions root under a locked
  folder, an env file, a vault slot or the env inventory that will not read or parse, a store that will not
  open; vault and env holes also raise `secret.leak_scan_blind`, even when they are the only sources of values
  (the "nothing to look for" row then names them). The env inventory is read as a regular file only: a FIFO,
  a symbolic link (dangling, a loop, into a locked folder), a wrong JSON shape, or a `path` that is absolute
  or climbs out of the data folder is a hole, never a hang, a crash or a read of `/etc/hosts`. So is a
  listed file that is a FIFO or a link (read through `lstat`, `O_NOFOLLOW|O_NONBLOCK`, `fstat`), a path with
  a NUL, a path that resolves outside the data folder through a linked folder, and an inventory nested
  past the parser's depth; a byte-order mark is accepted. The no-values reason says when values were found
  but none is searchable, or that the inventory lists no secret and the vault holds no slot. The env file read
  is checked by the open descriptor's canonical path, so a folder swapped for a link mid-scan is a hole; a
  lone surrogate in an inventory string is a shape hole. `scan_env` inventories the first line after a BOM. Round 8: the
  descriptor's folder is checked by identity (device and inode), so a traverse-only data folder (0311) is
  verified and nothing falls back to `realpath`; round 9: each ancestor is read with `lstat`, so a folder
  swapped for a link to the data folder after the open is not followed into it; a lone surrogate in any report or state string is escaped.
  A hard link to an outside file cannot be seen by a path check (COMPATIBILITY). **Known gaps, not closed here:** a PEM key stored with escaped newlines in a JSONL line, and base64- or
  JSON-escaped echoes of a value, are not matched (PB-157); a crowd of noise sightings can push a real one past
  the six the finding lists; a sighting is reported by the pass that first reads its row or offset only.
- **A prefixed token is a secret and a UUID is decided by its name** (INB-002): the env classifier read an
  issuer prefix (`ntn_`, `ol_api_`) as a word, so Notion and Outline tokens were filed as configuration, and a
  collection UUID as a secret. A prefix of at most eight characters in front of a random run of twenty or more
  letters and digits no longer reads as words. Under an identifier's name (`*_ID`, `*_PRICE*`, `*_RELEASE`,
  `*_TAG`, `*_COLLECTION`, `*_PARENT`, `*_PROJECT`, `*_ORG`, `*_BUCKET`) a value whose only randomness is that
  tail — a price, an organisation, a project, an image tag — stays configuration unless a known issuer prefix
  claims it; a name the secret rule also claims (`*_SECRET_ID`, `*_TOKEN_ID`) keeps it secret. A UUID is
  decided the same way, and under a name neither rule claims it is judged by its shape (the nil UUID is
  configuration). A random value under a secret's name, or behind a known issuer prefix, is a secret even when
  its body holds `-` or `_` (`GOCSPX-…`, `tvly-…`, URL-safe base64), which used to read as words about half the
  time. One prefix list: `KNOWN_PREFIX` now derives from `credential_shape.PREFIXES`, which gains `ntn_`,
  `ol_api_`, `secret_`, `GOCSPX-` and `tvly-`. The commit gate `tools/check_secrets.py` refuses every one of
  them (no word character or `_` before the prefix, snake-case names excepted, a vendor's own minimum body —
  `glpat-` 20). **The gate is an exact grammar** (`docs/design/AGENT-SECRETS.md`, "What the commit gate
  refuses"): one tokenizer reads a known issuer prefix (and `AKIA`/`ASIA`) at a token start — a literal `\n`,
  `\t` or `\r` escape included — with the longest `[A-Za-z0-9_-]` run, and refuses a body at or above the
  prefix's minimum whatever its entropy; a body of lower-case-letter `_` segments is a name, and two or more
  lower-case words or one repeated character is a placeholder, reported as a fixture. A named secret is an
  UPPER_SNAKE name ending in `_TOKEN`, `_KEY`, `_SECRET` or `_PASSWORD` as a quoted assignment, a `.env` line, a
  YAML line (a `- ` list item and a quoted name included) or a JSON member, whose value passes
  `scan_env.looks_secret`. **Out of the gate's scope, on purpose (OBS-94):** encoded values (`%XX`, `\uXXXX`,
  `\xXX`, ANSI codes) and an ASCII letter, digit or `_` before the prefix; values split by `\` continuation or implicit
  concatenation; code-level assignments (`os.environ[...] =`, Python dicts, Kubernetes name/value pairs, YAML
  block scalars, Go `NAME := "v"`, Python `NAME: str = "v"`, TypeScript `const NAME: string = "v"`); a `.env`
  value followed by `;`; lower-case names and a bare `KEY`; passwords that fail `looks_secret`; files outside
  the engine folder. Round 6 made the table exact: every tracked file is read as bytes (a non-UTF-8 file is
  judged, an unreadable one fails the gate), named-secret lines may end in CRLF, the AWS id has its own
  pattern (then anything but an ASCII letter or digit) and a token that is not judged lets the scan resume
  after its prefix, the bare-word rule measures from 20, a DSA or ENCRYPTED private-key header is refused, an
  `_`-joined placeholder is reported as a fixture, and a vendor minimum above 24 is honoured. Round 7: every
  rule judges every value and the most severe verdict per position wins, so `API_KEY="<40 hex>"` and a
  placeholder-led value are refused; the bare-word rule measures the whole value (no prefix stripped) and
  takes a JSON member keyed by the word; a BOM and lone CR line ends are read as lines. UTF-16 files are out of
  scope (OBS-94). Round 8: leading underscores are part of a name (`_API_KEY`, `self._API_KEY`); the bare-word
  rule also refuses a value that passes `scan_env.looks_secret` (a hex value past 35 characters cannot reach
  the 0.78 floor), reads a JSON `\/` as `/`, and does not judge a slot address (`project/env/NAME`, word
  segments without digits); `--history` reads git's output as bytes; U+2028, U+2029, NEL, FF and VT are line
  ends, a BOM at a line start is ignored, and a value may end in spaces inside its quotes. Round 9: a
  named secret's words may be joined by `__` (`APP__DB_PASSWORD`, `MYAPP__STRIPE__API_KEY`) and its first
  word may start with a digit (`_2FA_SECRET`), while a bare `_KEY`/`__KEY` stays out of scope; a value may
  begin after spaces or tabs inside its opening quote, and a YAML line may be indented by no-break spaces;
  a placeholder is a fixture before the shape test in the bare-word rule as in the named one; a slot
  address needs a name of two or more characters and may carry an upper-case project and a `_`-led name;
  the tree is listed with `git ls-files -z`, so a non-ASCII name is read and not reported unreadable;
  `--history` runs `git log -p --relative -- .` with unquoted names, reads a `+++` line as a header only
  between `diff --git` and the first `@@` (an added `++ b/x` or `++i;` line is a line), unquotes a C-quoted
  name and skips `SKIP`'s files, so `check_secrets.py --history 40` exits 0. Round 10: `--history` reads a
  file holding a NUL or marked `binary`/`-diff` as text (`--text`; git printed "Binary files differ" and its
  added lines went unscanned), diffs each merge's resolution against its first parent
  (`--diff-merges=first-parent`), and sets its own `a/`/`b/` prefixes, so a repository's `diff.noprefix`
  cannot hide a real `b/` folder; a slot name is as wide as the vault's (`APP__DB_PASSWORD`, `_2FA_SECRET`,
  `KEY_`). Round 11: `--history` also passes `--root` (a `log.showRoot=false` setting hid the root commit),
  `--full-history` (a side branch that added a key and removed it before its merge) and
  `GIT_NO_REPLACE_OBJECTS=1` (a `refs/replace/` stand-in), and unquotes a quoted name git ends with a TAB; a
  slot name is at most 128 characters, as in the vault; the tree scan reads a tracked symbolic link's target
  text and never follows it (a link to a FIFO blocked the gate), and reports any other non-regular file as
  unreadable; the bare-word rule's never-deciding "rest after a leading token" measure is gone. Round 13:
  a value of 32 or more hex characters under a named secret or a bare word is refused whatever its entropy
  (about 1% of 40-hex and 6.5% of 32-hex values passed under `looks_secret`'s floor); `--history` sets
  `GIT_GRAFT_FILE` to an empty file and passes `--no-renames`, so a graft cannot hide a commit and a file
  renamed or copied out of a `SKIP` path shows its content; the tree scan opens every folder on a path
  without following links (a path under a linked folder is unreadable), caps one read at 32 MB, skips and
  names a submodule, and reads a sparse-checkout or missing tracked file from its index blob; the text
  output gives each fixture's own reason. Round 14: every open of the tree scan and the leak scan goes
  through `osprivacy` (on Windows, which has no folder descriptor, each folder on the path is checked for a
  link first) and the leak scan reads a descriptor's path through `oslocks.fd_path`, so the engine's
  Windows/Linux boundary tests hold; an index blob is read without replace objects; `--history` refuses a
  shallow repository as incomplete (exit 1) rather than reporting clean; the table names the hex rule's
  false positives (public fingerprints, `*_PUBLIC_KEY`, `CACHE_KEY` hashes, `IDEMPOTENCY_KEY`) and the
  fixture convention for them. The known-value leak scan is their defence. Newly refused by the grammar, on purpose: a
  vendor's published example and `FAKE_…` bodies (compose such fixtures at run time). **Counts change** wherever the `secret` class is read: the SessionStart line, the leak scan's
  known values and `agent.secret_outside_vault`; the Env page and `observatory_credentials`;
  `env.tracked_in_git` severity, `env.world_readable` and
  `env.shared_secret`; `remote.same_as_local` and `remote.unbacked`; the agent-memory redaction set; and the
  survey's totals.
- **The 0.24.1 notes name the rotation they shipped** (ENG-017): "The collectors take turns" (OBS-52, #231)
  shipped in 0.24.1 but was missing from that release's notes, and the CHANGELOG packaged in 0.24.1–0.24.4 lists
  it under 0.24.0. The published notes were corrected on 2026-10-12 (the release files are unchanged); this file
  already lists it under 0.24.1.
- **`tools/backup_store.py` takes `--stamp YYYY-MM-DDTHHMMZ`** (ENG-008): a past or present minute names the copy,
  so the rotation's test no longer races the wall clock. A future minute, a malformed one, or one older than the
  three copies kept is refused with exit 2 before anything is copied; the tick never passes the flag.
- **The demo's leak finding dates its detail to the day its title counts back to**, instead of a fixed date that
  drifted from the title as the demo aged.
- **The private-size walk says when it could not measure** (ENG-006): a walk that ran out of time returned nothing,
  the row lost the figures the last walk had measured, and nothing said so; a place of few slow files never reached
  the deadline check. The walk now runs once per `private_every_hours` (`config/machine.json`, default 72), so the
  budget goes to `du`; `full machine --disk` always walks. A walk that times out, that the budget cannot fit, or
  whose place cannot be read keeps the last figures with their `frees_measured_at` (fitted inside the fresh size)
  and is named in the survey's `degraded`; the Machine page shows those figures "as of" their date.
- **No automatic removal from a carried git-hygiene row** (ENG-009): a hygiene pass the deadline cut short carries
  its unreached checkouts marked `stale`, and the cleanup planned automatic actions from them as from fresh rows.
  It now plans none from a carried checkout; its manual actions stay, marked `stale` with their `measured_at`. The
  plan records `survey_partial` and `carried` (count and oldest measurement), `cleanup` prints how many checkouts
  wait for a fresh pass (also in its `--json` summary, so the disk guard's report keeps it), and the Machine page
  and the `observatory_machine` overview show the partial survey.
- **Cleanup never removes local work it cannot restore** (OBS-84, OBS-86, #255):
  - a manual worktree removal skips a worktree that holds ignored files, before archiving and again before the
    removal (preserve or move them, then rerun `full cleanup --apply --include manual`);
  - `.wrangler` local state is no longer build output, even in an older copied configuration;
  - nested repositories, hidden index flags and bare repositories refuse removal;
  - a manual archive is read back, and the worktree's content, HEAD and index are checked again before removal;
  - a branch is deleted only at the tip that was proven, never through a symbolic branch;
  - every planned path carries its device and inode, and is checked again, with fresh holders, when it is applied.
- **Findings say when a measurement is incomplete or old** (OBS-88, OBS-89, OBS-90, #257):
  - `harness.scan_degraded` names an agent tool configuration the harness scan could not read; names recovered
    from malformed JSON or TOML are marked `parsed: false`, and a plaintext credential in a Git-tracked
    configuration is `critical`;
  - `tick.rotation_degraded` and `tick.collector_starved` name unknown collector scheduling and a planned
    collector that has not run; a disabled collector is not a measurement;
  - merged inputs keep their own measurement times (local inputs 2 hours, GitHub 6 hours, daily inputs 48 hours)
    instead of taking the merge's;
  - `machine.guard_stopping` warns when Git cleanup stopped in three consecutive disk guard runs, and
    `machine.guard_degraded` names what the guard could not measure; only a Trash permission cause gets the Full
    Disk Access remedy.
- **A failed suite keeps the others' receipts** (OBS-85, #255): the portable runner records an error row for the
  suite that failed in setup or report writing beside its peers' results, and never publishes a final PASS when
  writing the report failed.


## 0.24.4 — 2026-10-12

Inside the disk guard the git tier plans branches and worktrees only and fits its deadline, so a scheduled pass under load reaches its removals. The companion plugin `observatory-log` is unchanged.

### Fixed

- **Inside the guard, the git tier plans branches and worktrees only, inside its deadline** (OBS-63): on the first
  0.24.3 pass it planned 116 actions and reached none — its listing of what processes hold and a walk for build
  output (some 18,000 directories) took its whole share. The guard's own build-output class already covers that
  walk, so the guard asks for `--git-only`; the listing is bounded by the deadline; the result names
  `plan_seconds`.

## 0.24.3 — 2026-10-12

The disk guard's git tier removes merged and pushed branches and clean worktrees under load again: its re-check takes one listing of what processes hold per 20 seconds instead of one per worktree. The companion plugin `observatory-log` is unchanged.

### Fixed

- **The git tier's re-check takes one listing per 20 seconds, not one per worktree** (OBS-63): at load 300–700 each
  full listing of what processes hold took tens of seconds, and on 0.24.x the guard's git tier spent its whole share
  re-checking the first worktrees — no branch or worktree was removed by a scheduled pass. Run by hand on the same
  machine, the tier removed 95 merged and 3 pushed branches and 7 clean worktrees.

## 0.24.2 — 2026-10-12

A cache command never runs past its class's share, so the disk guard's git tier — merged and pushed branches, clean worktrees and their dependencies — keeps the time 0.24.1 set aside for it. The companion plugin `observatory-log` is unchanged.

### Fixed

- **A cache command never runs past its class's share** (OBS-63): on 0.24.1 three cache commands, each inside its
  own 90–300 s timeout, ran 271 s together and took the git tier's time again. A command's timeout is now what is
  left of the share, and none starts with less than 30 s left; the rest waits for the next pass.

## 0.24.1 — 2026-10-12

The disk guard's git tier — merged and pushed branches, clean worktrees and their dependencies — gets its own
share of every pass under disk pressure; on 0.24.0 the path classes could spend the whole run before it. The companion
plugin `observatory-log` is unchanged.

### Fixed

- **The disk guard's git tier keeps its own share of the run** (OBS-63): the first scheduled 0.24.0 pass, at
  `critical`, spent 425 of its 480 s on the six path classes and skipped the git tier — merged and pushed branches
  and clean worktrees with their dependencies, where most of the space is. The tier now has `git_tier_seconds`
  (120 s by default, at most a third of the run) kept back before the classes split the rest.

### Changed

- **The collectors take turns** (OBS-52): after `integrity`, the tick runs its collectors least recently started
  first, not in a fixed order. Under load the collectors in front used the window and the ones at the back were
  `not started` tick after tick: on one machine (load average 26–30) the leak scan and the lifecycle watch had not
  run for two days. A step that was not started keeps its place at the front of the next tick; a collector that
  reads another's output this tick still runs after it (`READS_FROM`, held equal to the artefact graph by
  `tests/test_pipeline.py`). Start times are kept in `store/tick-rotation.json`; `tick.json` reports
  `collector_order` and `not_started`.

## 0.24.0 — 2026-10-12

The disk guard keeps up with a disk that fills fast and never removes what a session still uses: the maintenance pass runs every 15 minutes, under pressure the guard acts on every pass, and a fresh, shared listing of what processes hold — open files, maps, executables, command lines — is re-checked at every removal, under one lock for every cleanup. One project answers to many names (aliases, `project.resolve`). The doors grow: `claim` brings a key minted elsewhere under the OpenRouter door, a project's own key comes from its spend policy, and Cloudflare gains `workers-routes` and `redirect-rules`. The meter grows: the estate's month measured, `grant.check` declared, a usage report. The harness scan names plaintext credentials in agent tools' configs. The companion plugin `observatory-log` is unchanged.

### Changed

- **`leaks`, `git-hygiene` and `agent` stop before their deadline instead of being killed** (OBS-52), reading
  `OBSERVATORY_STEP_DEADLINE` as the folder scan, `integrity` and the plugins already do:
  - `leaks` starts a file only while two minutes are left and a SQLite store only while one is. Offsets of what it
    read are kept; what it did not read is named in `degraded` as unmeasured, never clean, and a store's mark does
    not move.
  - `git-hygiene` measures the least recently measured checkouts first. The ones it did not reach carry their last
    measurement, marked `stale`, so no checkout starves.
  - `agent` starts a model call only while a whole call (150 s) fits, so a paid answer is never lost. The run
    report names the deadline, and the deltas wait for the next run.

- **Trust tiers, written down** (Fabric ADR-0144 amendment 4): on the operator's own machines (T0, and enrolled
  machines T1) the meter and the doors never demand a delegation token — `slotsRequireGrant: when-token` and
  `keyserverRequiresDelegation: off` stay the settings there; the strict ones are for a machine that serves another
  person's agents (T2). Design: `docs/design/METER.md` M0.

### Added

- **Cloudflare door: `--preset workers-routes --zone <zone>`** — Zone Read + Workers Routes Write on one zone,
  into a vault slot, verified by listing the zone's routes with the new value. `workers-edit` deploys a script
  but cannot route a hostname to it; this is that one zone right, without DNS. First use: moving a
  static site to a Worker.
- **`openrouter.py claim NAME --where PATH`, key on stdin** — brings an OpenRouter key this door did not mint (one
  made by hand in the dashboard) under the door, so `disable`, `limit` and `revoke` reach it. The value is hashed,
  never stored or printed; the provider's row is read by that hash, and a label alone (prefix plus three
  characters, which collide) is accepted only when it is the single match in every listing and the ceiling the
  key reports for itself agrees. A claimed key is revoked through the door, never rotated to nowhere.
  A claimed key is identified by its hash, so a rename in the provider's dashboard never reads as a deletion; a
  bulk `rotate` names it and goes on; `revoke` refuses it while a consumer reads its label; the label path needs
  every listing whole and a ceiling to compare; a claim re-checks the ledger it writes.

- **Cloudflare door: `--preset redirect-rules --zone <zone>`** — Zone Read + Dynamic URL Redirects Write (Single
  Redirects) on one zone, into a vault slot, verified by reading the zone's `http_request_dynamic_redirect`
  entrypoint with the new value. A zone with no redirect yet answers 404 there to a token that may read it and
  403 to one that may not, so for this preset alone a 404 counts as the proof.
- **A usage report** (OBS-53; contract DEC-0021): the server declares `surfaces.usage` and answers
  `GET /fabric/v1/usage` with the service token — the last 31 days of the assistant's paid model calls by provider
  and model, from a receipt written at every charge (tokens and cost with its basis, never a prompt or a caller).
  The fabric-service schemas are vendored again at contract `7931c1d`; the kit's conformance probe now runs
  `usage.report`.
- **Plaintext credentials in agent tools' configs** (OBS-74): the harness scan names, per file, the credential-shaped
  values kept by Hermes, opencode, Codex, Claude Code's MCP blocks, Gemini and Cursor — names only, never a value —
  as `harness.plaintext_secret`, with the vault and `use_secret.py` as the remedy. Design: `docs/design/HARNESS.md`
  H15.
- **A project's own key, from its spend policy** (OBS-58): `openrouter.py issue-for-policy --policy FILE` reads a
  contract `spend-policy` (DEC-0037) and issues, for each `project-key` source, a key for that project into the vault
  slot the source names. The key's ceiling is the project limit, and it resets monthly. A slot of another project is
  refused as a borrow (spend-policy rule 1). A policy with no project limit needs an explicit `--limit`, because a key
  is never unbounded. A ceiling above the project limit is refused. A second run is idempotent: it only moves a
  ceiling the policy changed.
- **`grant.check` is declared in `fabric-agent.json`** (OBS-67; manifest revision 9): effect `none`, its
  input and output schemas published at `v0.24.1`, a probe that a call carrying no delegation token is denied
  `principal-unproven`. Fabric's registry reads the meter from the service descriptor; the token-carrying call
  goes to the same tool on the memory-http door.
- **The estate's month, measured** (OBS-56): the new step `scan-spend` sums this month's spend of every OpenRouter
  account the door holds (`GET /activity`, per account, never a key) into `store/raw/spend.json`. The meter counts
  it, plus open reservations, against L0 (USD 1,000 by default), and the finding `spend.estate_month` warns at
  80 % and is critical at the limit. Measured on the operator's machine on 2026-10-11: $262.97 across four
  accounts.
- **The harness scan reads hooks and skill use** (OBS-61): every hook the user's settings and each enabled
  plugin declare (event, matcher, source, program, a digest of the command and never the command), checked
  against the skill rules, with the finding `harness.hook_risk`; and each skill's invocations over the last 30
  days of transcripts, so the listing overflow names the unused skills first; and each project's own
  `.claude/skills`, counted on top of the user level (`harness.project_listing_overflow` when they alone push a
  session over the budget); and Codex's skills (`~/.codex/skills` and the shared `~/.agents/skills`). Design:
  `docs/design/HARNESS.md` H11–H14.
- One project under many names (OBS-65). The operator curates `config/identifiers.json`
  (another system's name → the canonical `project:`/`product:` id, with the name's owner); the
  emitter publishes `registry/identifiers.json` and one `known_as` relation per alias. A name
  given two targets, or contradicting a measured repository, deployment or domain, is a
  published conflict, never a silent pick. `project.resolve` (MCP `observatory_resolve`, Fabric
  capability pinned at `v0.24.0`) answers any name with the one canonical id, exact match only;
  an ambiguous or conflicted name is a typed refusal. Agents propose aliases through
  `observatory_propose` (`aliases`); accepting one never rewrites an existing row.

### Fixed
- **The disk guard frees more, on any system** (OBS-63): its pressure now lowers the git auto tier's thresholds
  (`pressure` in `config/cleanup.json`: clean worktrees and merged branches after 3 days at `low`, 1 day at
  `critical`; the manual tier never moves), it sweeps npx's package cache and macOS diagnostic reports, and it
  knows what is held on Linux without `lsof` (from `/proc`) and on Windows (a folder that cannot be renamed aside is
  held, and nothing of it is removed) — before, without `lsof` it removed no path at all.
- **The disk guard keeps up with a disk that fills fast** (OBS-63): the maintenance pass runs every 15 minutes
  (it was hourly), and the guard acts on every pass once free space is below the `low` line (hourly while it is
  normal); it now also clears application and tool cache folders (`~/Library/Caches`, `~/.cache`) untouched for
  7 / 2 / 1 days — the folders a hand cleanup freed 10 GB from — keeping downloaded browsers and SDKs a tool does
  not fetch again by itself (`cache_keep`); and its per-class cap is 20,000, since the run budget bounds its time.
  Measured on one machine: free space fell from 6 GB to 0.3 GB inside one hourly gap.
- **The disk guard finishes inside its pass** (OBS-63): the first scheduled pass on 0.23.0 killed it at the
  job's 1200 s ceiling, under launchd's background priority, and recorded only `exit 124` — no report, no
  journal. The job's ceiling is now 600 s and the guard stops itself at `--budget` (the job passes 480 s;
  `run_budget_seconds`, 900 s, by hand), shares that time fairly between its classes, acts before it refreshes
  any survey, writes each action to `store/logs/disk-guard.jsonl` as it happens, and always writes its report,
  naming what the budget left for the next pass.
- **The disk guard, second iteration** (OBS-63; design `docs/design/DISK-GUARD.md`): a review of the first one
  found it could remove what a session still used, and could misreport what it did. Now:
  - *Held* means open files, memory maps, executables, working and root directories and paths named on a
    process's command line (`holders.py`: `lsof` with its escaped names decoded, `/proc` on Linux, a rename probe
    on Windows), taken fresh for every class; every removal re-checks its target, still unheld and unchanged since
    the plan, and a target that changed is skipped.
  - Every cleanup takes one lock (`store/cleanup.lock`): the tick, the guard and `full cleanup` never act at
    once, and a guard pass that finds the lock held is recorded as `busy`.
  - A privacy-guarded folder is never opened, an entry another account owns is never touched, and a tracked
    directory is never removed as build output.
  - The git tier no longer removes a worktree holding ignored files that are not build output (`.env.local`, a
    local database); pressure lowers only thresholds a person did not set; a run stopped at its deadline keeps its
    undo journal; `full cleanup --level low` previews the tier at a pressure level.
  - A workspace `config/cleanup.json` that only carries an older release's defaults no longer pins them.
  - The pass records what happened — `ran`, `not-due`, `busy` or `failed` — not the previous run's numbers; the
    guard's cadence counts from the previous start, with a 450 s tolerance; `uv cache clean` and `brew cleanup
    --prune=all` run once a day, other cache commands every six hours.
  - A failed daily backup is retried an hour later, not on every pass (each attempt stops the tick and server);
    Windows ends a pass after 59 minutes instead of 14; an update that rewrote the schedule reloads it after the
    pass instead of waiting for a session.
  - Machine page and the `cleanup.done` finding count the guard's run summaries over the whole window.
  - pytest's numbered run directories (`$TMPDIR/pytest-of-<user>/pytest-N`) go by their own age in hours
    (24 / 6 / 2): their base is never older than the newest run, so the guard never reached them — 2.5 GB of
    finished runs sat there on 2026-10-12 with 1.2 GB free. A temporary root may carry `hours` per level.

- `_published_shape` follows local `$ref`s and cuts a `oneOf` by the branch whose required
  keys the value carries; before, such an answer passed through uncut.

## 0.23.0 — 2026-10-11

The disk guard keeps free space by itself, every hour, without touching anything a process holds; the meter
answers whether a delegated call may spend; a vault slot is released only under a grant that names it; and the
harness scan reports what every agent session loads. Releases are now approved by checks, not by a click. The
companion plugin `observatory-log` is unchanged.

### Added

- **The disk guard** (OBS-63): with `features.auto_cleanup` on, the hourly maintenance job runs
  `tools/disk_guard.py` first in every pass, before the update and the backup — no longer only from the tick,
  whose cleanup step a loaded machine could leave unreached for a day. Free space sets the pressure (`normal`,
  `low` under 25 GB, `critical` under 10 GB) and the pressure moves every threshold: the cache tools' own cleanup
  (`uv`, `pnpm`, Homebrew, Docker only while its daemon already runs; `npm cache verify`, `uv cache clean`, Go and
  Yarn from `low`), git-ignored build output of checkouts idle 14 / 3 / 1 days (by checkout, commit or an
  uncommitted file, never the index), temporary entries, Trash items older than 30 days (3 at `critical`),
  abandoned browser code-sign clones, then the cleanup's git auto tier. Nothing a process holds open or works in
  is removed: one `lsof` listing per run, re-checked at each removal, and without it no path goes. Freed space is
  the disk's own before/after count (`freed_mb`), not `du`, which counts an APFS clone in full.
  `full disk-guard [--apply] [--level L] [--large-files]` runs it by hand. The Machine page and
  `observatory_machine` (`section: guard`) show the last run, the largest files under the home directory and the
  running simulators with the command that stops each; `machine.guard_idle` warns when the guard has not reported
  for three hours, and `machine.disk_low` names the guard's run, the swap holders and the largest files.
  Configuration: `config/cleanup.json` → `guard`. Supersedes the Space-page draft (OBS-15).

- **The meter** (OBS-55; Fabric ADR-0144 step 3, contract DEC-0037/0038/0039): `grant.check` on the memory-http
  door answers whether a call may spend. The caller presents the onward delegation token Fabric's hub minted for
  `project-observatory` in `Authorization` (never in an argument); the meter verifies it offline against the
  estate's Ed25519 keys (`delegation.py`, the contract's verifier table DL-13), reads the grant from the hub's
  grant directory, and reserves the amount against the grant's `call`, `task`, `total`, `project`, `day`, `week`
  and `month` windows and the estate's monthly total (L0, USD 1,000 by default) and provider-key ceilings, in one
  transaction. It answers a receipt, or a denial naming the code, the level and the window. `full grant --status`,
  `--check` (token on stdin) and `--settle`. The ledger records no person and no chain. Configuration:
  `config/meter.json`. Design: `docs/design/METER.md`.
- **The meter reads the hub's grant projection strictly** (contract DEC-0041, `readProjectedGrant`): a projection
  that is not the owner's alone (0700/0600), a link or FIFO (one `open` with `O_NOFOLLOW` and `O_NONBLOCK`), over
  1 MiB, outside the standing-grant shape, with times that are not RFC 3339 with a zone, behind the token's
  revision or of another grant is `unknown-state`; a revised grant is `no-grant`; Windows is `unknown-state` until
  the profile covers it. A malformed `task` claim refuses the token. The contract schemas are vendored again at
  `7931c1d` (the `call` window and the `task` claim are in them now).
- **A vault slot only under a grant** (OBS-57): an agent hands `use_secret.py` its delegation token in
  `FABRIC_DELEGATION_TOKEN` (scope `secret.use`). `run`, `serve` and `header` then release a slot only when a grant
  the token names has `credentialSource {kind: project-key, ref: PROJECT/ENV/NAME}`, never read a `.env`, and
  remove the token before the child starts. `slotsRequireGrant` in `config/meter.json`: `when-token` (default
  on the operator's own machines; METER.md M0), `always`, `off`.
- **The keyserver trusts the delegation token** (OBS-62; corrected after release: its code, #206, merged after
  the v0.23.0 tag and first shipped in 0.24.0, not in this release): with a valid token for the action's scope (`keys.*`,
  `ledger.*`), the journal names the hub's signed caller, principal, chain and grant (`caller_verified: true`).
  `reveal` is never served to a token. `keyserverRequiresDelegation: agents` demands a token from non-page callers.
- **The agent harness scan** (`integrations.harness`, `sources.harness_root`; off until turned on): what every
  Claude Code and Codex session loads before the first word. It counts each skill's entry in the session's skill
  listing against a budget (`OBSERVATORY_HARNESS_BUDGET_CHARS`, default 30,000 characters) and names the
  descriptions that no longer fit, largest first; flags an instruction file over 200 lines and the settings that
  load every MCP tool upfront; and runs deterministic rules over skill files — hidden Unicode, concealment
  directives, download-and-execute, a credential read beside a send, opaque content — weighed by where the skill
  comes from. Files are read, never run. A skill whose files change is reported until
  `full harness --accept KEY`; an ack of a rule hit lapses when the skill changes. Design: `docs/design/HARNESS.md`.

### Fixed

- `full update` refusing with "the workspace changed while staged" now names the paths that moved, so the writer
  to stop can be found (#200).

## 0.22.0 — 2026-10-11

Project Observatory on Windows and Linux: a desktop app with a signed Windows installer, the tick, the server
and maintenance as per-user Task Scheduler tasks or systemd units, updates that work on Windows, and the backup
passphrase in the Credential Manager. The Mac app is universal again for Intel Macs. The companion plugin
`observatory-log` moves to 0.22.0.

### Changed

- **The vendored fabric-service kit is fabric-agent-adapter 0.8.2** (was 0.5.0): Windows and Linux support
  (contract DEC-0032, DEC-0033) — the instance lock, the token-file ACL rule, Task Scheduler and systemd in
  the descriptor — which the always-on server needs on Windows (W4b). `serverd --uninstall` relies on the
  kit's own wait for launchd to unload the job. The conformance probe's two new checks the server does not
  declare yet are NOT_RUN (`usage.report`: OBS-53).

### Added

- **The Windows and Linux app** (W8a, `desktop/`, Tauri 2): opens on this workspace's dashboard — live when its
  server answers, else the built pages under a banner with «Start server», else «Build the dashboard»; Settings
  for the program, the workspace and the language; first-run messages that name the install or `full init`
  step; stays in the tray when its window closes. Built as an NSIS installer and an AppImage/.deb by
  `.github/workflows/desktop.yml`; the release builds and Authenticode-signs it (W9). It updates itself from the
  release's `latest.json`, each package minisign-signed: «Restart to update» in the menu and the tray (W8c). The
  assistant window (Ctrl+Shift+A) asks the workspace's assistant, with each answer's evidence and Stop (W8b).
- **Updates on Windows** (W6): `full update --apply` stops and restarts the tick and server tasks (systemd units
  on Linux) around the install, and replaces the `project-observatory.exe` it may be running from — moved aside
  first, put back if the install fails.
- **The server's always-on state off macOS** (W4c): «Start server» in an app restarts the Task Scheduler task
  or systemd unit of an installed server instead of calling `launchctl`; `server.silent` watches that
  installation too.
- **Windows paths and tools** (W7): the default workspace is `%LOCALAPPDATA%\project-observatory-full` (an
  existing `~\.local\share` one stays); tools npm installs as `.cmd` (`heroku`, `claude`) start by name; the
  CLI and the scheduled jobs write UTF-8. README → Install has the PowerShell steps.
- **The backup passphrase in the Windows Credential Manager** (W5): a generic credential per workspace, kept
  for this account on this machine (`CredWriteW`/`CredReadW`, never roaming, the value never an argument);
  a store that does not answer is not an empty one. The file fallback is owner-only on Windows too (a
  protected ACL instead of mode bits).
- **`osschedule`: background jobs under Windows Task Scheduler and systemd** (W4a). One job description, a
  per-user task created from XML (`InteractiveToken`, no stored password, `pythonw.exe` through
  `tools/scheduled.py`, so no console window opens) or a systemd user unit; `tools/install_launchd.py` and
  `tools/serverd.py --install` use it off macOS, the server's fabric-service descriptor naming `task-scheduler`
  or `systemd` (W4b).

### Fixed

- **The Mac app is universal.** `macos/scripts/build-app.sh` builds `arm64` and `x86_64` and refuses an
  executable missing either slice; up to 0.21.0 the release carried the runner's architecture only, so
  the app did not open on an Intel Mac (PassionCode.ai platforms rule PL-01).

## 0.21.0 — 2026-10-10

The engine runs on Windows, and a check of the agent on a heavily loaded machine fixed what it
said wrongly about itself and what kept its own tail from running. The companion plugin
`observatory-log` moves to 0.21.0.

### Added

- **The engine on Windows** (docs/design/WINDOWS-LINUX.md, W0–W3). One module per OS seam, each
  with a test that runs the same on every OS and refuses the POSIX-only call anywhere else:
  `oslocks` (file locks — `LockFileEx` on Windows), `osproc` (process groups, the stop signals an
  OS has, the assistant's deadline without `SIGALRM`; on Windows `os.kill(pid, 0)` terminates the
  process, so checking a job killed it), `osprivacy` (owner-only files through the file's ACL;
  descriptors opened in binary mode, without which Windows turns every LF into CRLF). The tick is
  Python (`tools/tick.py`; `tools/tick.sh` hands over to it), and a whole tick runs on the
  `windows` CI job. Windows needs Python 3.13 or newer.

### Fixed

- **A busy machine no longer reads as a blank dashboard.** Under a load average near 300 the
  page's script did not finish within 10 s and smoke reported a critical `dashboard.blank` about a
  page that renders in 0.13 s of processor. A timed-out script is blank only when it has used a
  5 s processor budget; a starved one is tried once more with 60 s, then recorded as not measured
  (`dashboard.unverified`, information).
- **The tail's cheap optional steps run before the heavy ones.** The index, retention (the
  erasure), the fixture sweep, corroboration, the ledger export and the lost-projects record —
  seconds each — were not started tick after tick behind `plugins` and the agent's model calls.
  What the agent proposes is now indexed and exported on the next tick.
- **A plugin stopped at its limit keeps what it measured**, and is told its deadline
  (`OBSERVATORY_STEP_DEADLINE`). `disk-usage` walked every tree twice and lost every row at the
  300 s limit, so it ran again on every tick and cost the tail 300 s each half hour; it now walks
  each folder once, measures the projects measured longest ago first and stops between projects
  (51 s for this machine's estate).
- **The store's integrity check is bounded.** `integrity_check` runs once a day and after any
  verdict that was not `ok`, `quick_check` in between; the step stops itself at its deadline and
  records `unmeasured` (a `store.integrity_unmeasured` row naming the last full pass) instead of
  being killed with nothing written. Its limit is 240 s: one tick spent 785 s there and left the
  collectors no time.
- **The public-release check reads engine paths the same on Windows** (`as_posix()`).

## 0.20.1 — 2026-10-09

A patch release from a read-only audit of 0.20.0 running on a working machine. The companion plugin
`observatory-log` moves to 0.20.1.

### Fixed

- **Findings and the dashboard run again on a slow machine.** No tick had finished for 13 hours:
  with the collectors held back, the tail's optional steps — plugins, rollup, the agent's model
  calls, the vector index, retention and review — still ran before the findings and the dashboard
  and used the last of the ceiling (14 ticks in a row ended inside `plugins` and `agent`). They now
  stop two minutes before the ceiling (at most a twelfth of it), leaving the core — from the merge
  to the findings, the dashboard, the notice and the registry commit — its time; a step that did
  not run leaves the previous run's output.
- **Health says why a tick did not finish.** A tick the supervisor stopped at its ceiling was
  reported as "killed (a restart, a sleep, a signal)"; the supervisor's own record is read now.
- **The 3-of-10 unfinished-ticks alert reaches the service card and `doctor`**, not only the MCP
  survey (`tick.history_warning`).
- **A restored workspace keeps its files' times.** A rollback dated every file at the restore,
  and the tick's daily refreshes slipped by the time since the snapshot.
- **The maintenance record follows a person's update.** After a manual `full update` it still
  named the last automatic one beside the newer engine; it now records `updated-by-person`.
- **The changelog's 0.19.3 section has its heading back** — 0.19.4's version bump had overwritten
  it — and a test now refuses a gap or a repeat among a minor version's headings.

### Documentation

- The README says how the Mac app keeps itself current; the onboarding guide describes how the
  tick's ceiling is shared out and what `tick.recent` and `tick.history_warning` mean; `AGENTS.md`
  states the app's 15-minute check.

## 0.20.0 — 2026-10-08

A minor release, and it **changes behaviour**: the Mac app shows a waiting update sooner and
where it is seen without a window, Health counts the ticks that did not finish, and the full
engine's dependencies move (`mcp` 2.3.0). The companion plugin `observatory-log` moves to 0.20.0.

### Changed

- **A waiting update is visible.** The Mac app reads the maintenance record every 15 minutes
  instead of every 6 hours, so **Restart to update** appears within the hour the update is
  staged, and the Dock icon carries a `↑` badge until it is installed — seen without opening a
  window. Left alone for 30 minutes with no window on screen, the app still restarts into the
  update by itself; an app older than 0.19.0 cannot, and is replaced the first time it is quit.
- **Health says how many recent ticks did not finish.** Each tick's outcome is kept
  (`store/raw/tick-runs.jsonl`, the last 50); Health and `doctor` summarise the last 10 and
  report a degradation when 3 or more stopped at the ceiling or failed (OBS-40).
- **Dependencies:** `mcp` 2.3.0, `google-auth` 2.59.1, `cryptography` 50.0.2, with `pydantic`
  2.14.0 and their dependencies, through the refreshed `requirements-full.lock`. The memory
  service's HTTP transport is pinned to the SDK it was measured on, so the move was measured
  again: every property is unchanged (`docs/runs/2026-10-08-http-transport-repin`).

### Fixed

- **`observatory_credentials` lists every vault folder of a project** — a folder named after the
  project's name was missing, while `use_secret names` already listed it (OBS-26).
- **A Dependabot bump no longer fails CI at install with an unnamed cause.** `tools/refresh_lock.py
  --check` runs first and names the stale pin; `tools/refresh_lock.py` rebuilds the lock (OBS-17).
- **A failed `backups decrypt` no longer leaves the decrypted copy behind.** When moving the
  decrypted workspace to its destination failed, its hidden staging folder — plaintext
  secrets included — stayed on disk; it is now removed and the error reported.
- **A git call that fails to write its helper file no longer leaks a temporary folder** — on a
  full disk, the long-running server lost one per call.
- **Tests that skipped while CI stayed green now run (OBS-42):** the documentation check, the
  design-pack audit of the projects row, the two identity finding rules and the path rules
  (`tools/check_paths.py` is clean, and `test_api_listings` asserts it).

## 0.19.4 — 2026-10-08

A patch release for two races found while installing 0.19.3 by hand on a working machine: the
update rolled back safely, and a second try installed it.

### Fixed

- **The hourly pass no longer touches the Mac app while an engine update runs.** A person's
  `full update` and the maintenance pass ran together; the pass's app step downloaded the new
  app into `store/app-update` and removed its checksum files while the update was snapshotting
  the workspace, and the update rolled back on the missing file. The pass, and the step the app
  starts after it quits, now wait for the next pass while an update holds its lock (the daily
  snapshot already deferred itself).
- **A job found running after an update is restarted on the new code.** While the update had
  the server stopped, something loaded it again; the update's own start was then refused as
  "already loaded", it asked a person to run `launchctl bootstrap`, and the server kept the code
  it had started with. A job found loaded is now restarted (`launchctl kickstart -k`).

## 0.19.3 — 2026-10-08

A patch release: four defects a tester found in 0.19.1 on a workspace with only the projects
source on and every integration off — the service card stayed yellow and no daily backup was
ever written. It carries 0.19.2, which was tagged but not published. The companion plugin
`observatory-log` moves to 0.19.3.

### Fixed

- **The tick and the server run on the engine's own interpreter.** Their launchd jobs were
  written with the interpreter of whatever process installed them, not the engine's virtual
  environment; started from another python, the tick ran on Homebrew's bare python and its
  backup step failed every time with `No module named 'cryptography'` — installing the package
  into the venv could not help. Both now name the venv's python, as the maintenance job already
  did. An install whose job files were written before this is repaired by the hourly
  maintenance pass: it rewrites the interpreter in the tick's and the server's job and reloads
  them — the tick only while it is not running.
- **Switched-off integrations no longer make the service look degraded.** Health counted the
  merge's inputs whose integration is off (wiki, GitHub, sessions, git remotes, Bitbucket) and the
  repository-transfer checks that GitHub being off cannot ask as sources "not fully read". They
  are now treated as the merge's own summary already called them — "not measured, switched
  off" — on the card and on the board alike.
- **The lost-projects step is skipped while sessions are off.** It reads only what the sessions
  collector writes, and failed every tick without it.
- **The registry no longer fails its own validation over a suggested product.** Products the
  engine suggests from a shared domain give members the role `other`, which the document did
  not declare unless the operator's roles happened to include it; with GitHub on, the tick
  stopped at `validate`. The suggested role is now always declared; curated products are still
  checked against the operator's roles.
- **The `ownership` row says where to declare an organization**: `organizations` in the
  workspace's `config/ownership.json`, now described in the onboarding guide.

## 0.19.2 — 2026-10-08 (tagged, not published; included in 0.19.3)

A patch release: four defects found on a working machine, and the documentation brought back in
line with the code. The companion plugin `observatory-log` moves to 0.19.2 with it.

### Fixed

- **A slow tick no longer ends without findings or a dashboard (OBS-40).** On a machine short of
  disk and memory, the folder scan used its whole 900 s limit and was stopped with nothing
  written; the tick then reached its ceiling before the registry, the findings and the dashboard
  ran — 42 such stops in one machine's `tick.err`; the folder scan alone reached its 900 s limit
  14 times between 4 and 7 October. Collectors now stop 300 s before the ceiling (at most a fifth
  of it) so the steps from `merge` on always get their time, and every step is told its own
  deadline (`OBSERVATORY_STEP_DEADLINE`). The folder scan uses it: out of time, it writes what it
  measured and carries each folder it did not reach from the previous scan, marked `carried_from`
  with the date it was really measured, and says how many in one degradation.
- **A read-only copy of the store can no longer miss a write made during it (OBS-41).** A reader
  that found the store idle opened it without locks; a session that wrote while it read was not
  seen, and the copy was whole but stale. Such a read is now kept only if the store's files are
  unchanged afterwards, and read again otherwise — for snapshots, backups, the integrity check
  and the version check before opening.
- **An update no longer fails because a session hook wrote its receipt.** 0.19.1 let the store
  and the logs change while an update is staged, but not the receipts the session hooks write on
  every agent turn (`store/raw/record-turn.json`, `sessions-seen.jsonl`,
  `companion-faults.jsonl`); any of them still refused the update. They may now change; settings,
  secrets and registries must still hold still.
- **`doctor` no longer asks for the claude-mem database.** The `sessions` integration reads the
  Stop hook's own records first and the companion's database only where it is installed, but
  `doctor` listed a missing `companion_db` as a coverage gap. It is now optional, and only a
  configured path that is not a file is reported.

### Documentation

- `SECURITY.md` says the backup passphrase is kept in the login Keychain, and that the maintenance
  job checks for and installs updates by default, with how to turn it off.
- The README, onboarding, compatibility, migration and Mac app pages were corrected against the
  code: the backups location, signed updates, the `full update` exit code 6, the second-machine
  install, the variables `agent install` writes, and the app's language and update behaviour.

## 0.19.1 — 2026-10-07

A patch release. 0.19.0 could not be installed over 0.18.0 on a machine where agents are working:
the update rolled back with "Workspace changed while upgrade was staged".

### Fixed

- **An update no longer fails because agents are writing.** The workspace upgrade replaced the
  store with a copy it had migrated aside, so one write by any Claude Code session (its Stop
  hook or MCP server) while the copy was prepared refused the whole update — and had it gone
  through, that write would have been lost. The copy now only proves the migration runs; the
  live store is migrated in place in one SQLite transaction, which concurrent writers wait for,
  and only the configuration files must hold still. A snapshot is still taken first.

## 0.19.0 — 2026-10-07

A minor release, and it **changes behaviour**: updates follow the organization's one update
behaviour (LC-16) — a check every six hours, a switch file, activation only when nobody is using
the observatory, held releases — and the Mac app installs its own updates. The dashboard and the
Mac app follow the reader's system language and the organization's glossary. The companion
plugin `observatory-log` moves to 0.19.0.

### Changed

- **The agents' records ask nothing of you.** Observations, notes and sessions agents write
  are their working memory: an independent source confirms one, or it is retired after 90
  days. The Health page no longer shows them as "Awaiting a person's decision" with accept and
  reject buttons on every row; a section "Agents' records" says nothing there needs you and
  folds the newest rows, and the overview card no longer counts them.

- **The Mac app installs its own updates.** When a new version is ready while the app is
  open, the app menu and the dashboard's toolbar offer **Restart to update**. Choosing it
  restarts the app on the new version. Quitting the app installs the waiting version too, and
  so does leaving the app with no window open and untouched for 30 minutes; it then reopens in
  the background. If the update cannot be installed, the previous version opens again. Until
  now a person who kept the app open never got an update. Each step is logged in
  `~/Library/Logs/Project Observatory/app.log`. `project-observatory full maintain app` runs
  only the app step of the hourly job.
- **The Mac app speaks Russian.** Menus, both windows, alerts and Settings open in Russian
  when Russian is the first language in System Settings. Settings → Language now offers
  System, English and Русский, and the choice is kept across updates. Counts use real Russian
  plural forms.
- **The dashboard follows the reader's system language.** It opens in Russian when the first
  preferred system language is Russian and in English otherwise, whatever language the pages
  were built in. The language switch now offers System, English and Русский; a reader's
  choice is kept in the browser and survives rebuilds and updates, and System hands the
  decision back to the system. `interface.locale` is now the language of a build that no
  browser reads (the CLI, the session hook, the checks) and of a browser that reports no
  language.
- **Russian terms follow the organisation's glossary.** A workflow is «задача» (a background
  job is «задание»), a backup «резервная копия», the vault «хранилище», an account «аккаунт».
- **Dates and numbers follow the language.** Russian pages write dates as `02.01.2026` and
  numbers with a decimal comma; sizes on the Machine page carry their unit in the reader's
  language.
- **Refusals and degraded reasons are translated where they are shown.** Text the engine
  writes in English reads in Russian when the catalog has it, and in English otherwise.

### Fixed

- **The Dock no longer draws a blank icon for the app.** The updater stages the new bundle and
  keeps the retired one; macOS registers every bundle it sees, and one deleted while still
  registered stayed the app's record. Each bundle is now unregistered before its folder is
  removed.
- **An automatic update waits for a running tick** instead of stopping it halfway, as the daily
  backup already did.
- **Every line the local server writes to `serverd.err` starts with its time** (UTC).
- A page built in one language and read in the other no longer keeps words, units, dates or
  tooltips in the build's language on the Machine and Agents pages.
- **Updates behave as in every other product of the organization** (lifecycle LC-16). This
  changes behaviour:
  - **The switch is a file.** "Install updates automatically" is the file `auto-update` in the
    workspace folder: without it updates are on, and only the word `off` in it turns them off.
    `full auto-update on|off` writes it. An update, a reinstall or an uninstall never writes it.
    If you turned updates off in 0.17 or 0.18 (`updates.auto: false` in `settings.json`), they
    stay off, and the next `full auto-update` command moves that choice into the file and says
    so. Off now also stops the Mac app's update.
  - **A check every six hours** instead of once a day. The first check comes 90 seconds after
    the job starts at login. A check that could not reach GitHub is tried once more within the
    hour, then every six hours again.
  - **A new release starts running only when nobody is using it.** The job installs an update
    when the local server has served no page or agent, and no memory-over-HTTP client has
    called, for five minutes. Otherwise the release waits as "ready" and the next hourly pass
    tries again. It also waits while a tick is running, which an automatic update used to stop
    half-way. `full update --apply` by hand still installs at once. A running Claude Code
    session is never stopped by an update.
  - **A release that needs a person waits for one.** When a release declares a step to do by
    hand (`RELEASE.json` inside its signed wheel), the job downloads and verifies it, and
    `doctor`, the Health page and `auto-update status` show the step. It is not installed until
    you run `full update --apply`, which shows the step first.
  - **An installed release reads only the organization's feed.** `OBSERVATORY_RELEASE_REPOSITORY`
    and `OBSERVATORY_RELEASE_API` now work only from a source checkout; an installed release
    ignores them and says so. `--repository` and `--api-url` still choose a fork or a mirror.
  - **The Mac app is installed only when signed by the organization's team** (`KJ35UYYL22`),
    whoever signed the copy already installed. An installed app with no team is still left
    alone.
- **Every update stage is logged by code** in `~/Library/Logs/Project Observatory/updates.jsonl`
  (`~/.local/state/project-observatory/logs/` on Linux): checks, downloads, installs, restarts
  and the switch. Codes only, with no version, path or message.

## 0.18.0 — 2026-10-06

A minor release, and it **changes behaviour**: an automatic or manual update installs only a
release whose `SHA256SUMS` is signed by the organization's release key. It carries the fixes
from a full audit of 0.17.3 — code, interface and every documented scenario — and from two
independent reviews of those fixes ([run record](docs/runs/2026-10-05-audit/README.md)). The
companion plugin `observatory-log` moves to 0.18.0.

### Changed

- **Updates are signed** (A04). `full update --apply` and the Mac app's swap check
  `SHA256SUMS.asc` against the release key pinned in the engine (`release_signature.py`)
  before anything changes. An unsigned release, a signature by another key, a signature over
  other bytes or a malformed one is refused. The rollback wheel of the running release must
  be signed too, for every release from 0.14.0 (the first one published with a signature).
  Without `cryptography`, a plain-Python Ed25519 check, tested against RFC 8032, does the work.
- **The backup passphrase is never replaced by a guess** (A01). A store outside the workspace
  that does not answer (a Keychain timeout, a locked keychain, no keyring) is no longer read as
  "no passphrase": nothing is generated, and Health says the backups wait. A first copy never
  overwrites an existing one. Every earlier passphrase is kept, and a restore tries each.
- **OpenRouter keys can reset daily or weekly** (#175). `openrouter.py issue --reset
  daily|weekly|monthly` and `limit <name> --set N [--reset …]`; the dashboard's mint takes
  `limit_reset`. A per-day ceiling is what paid fallback agents run under.
- **Cloudflare tokens get about 80 seconds to be honoured** before their proof fails, instead
  of 10. The waits double from 2 s. A new R2 pair was refused twice for a slow edge.

### Fixed

- **Automatic updates on Linux** no longer refuse every day (A02). The update runs in its own
  session, so a stopped pass, a SIGTERM or `maintain uninstall` no longer kills it halfway
  (A03). On Linux the unit uses `KillMode=process` for the same reason.
- **A rolled-back update is recorded as rolled back.** Its output and its last journal line no
  longer go into the copy the rollback moves aside.
- **The daily snapshot and a person's update never run at once** (A12): the snapshot holds the
  update lock while the jobs are down.
- **Restore** names a command that works and keeps the passphrase off the command line (A05).
  With backups from several workspaces it lists them instead of picking one (A06).
- **Stale state is cleared** (A13). A GitHub token that GitHub refuses is dropped for the rest
  of the run, said once.
- **Tests never touch the machine or the live workspace** (A07, A08, A25). They inherited
  `OBSERVATORY_HOME` from an agent's shell and could read the live workspace. `maintain run` and
  `maintain hook` now ask the same consent as `maintain ensure`.
- **Fresh workspaces:** a declared key in an empty vault reads `missing`, with the command that
  fills it, and an `unknown` one appears in "Needs you" (A10).
- **Snapshots, memory over HTTP, the hook, backups** (A11, A15, A17–A22). Snapshots tolerate
  session servers writing. A flood without a bearer no longer locks every client out of
  `memory-http`. The hook's claim is race-free. `forget` names plaintext copies that hold the
  erased text. `.failed-update-*` copies are reported. The pass rotates its own logs. Another
  user's running app no longer blocks the swap.
- **OpenRouter door:** `issue` works on an account with more than 20 000 keys (#167). A ceiling
  move is recorded in the movements journal. After `rotate --leaked`, both doors name
  `vault.py settle`, which closes the leak, not `vault.py rotate`.
- **`use_secret.py names`** lists every vault folder of a project, not only the one typed.
- **Moving a ceiling no longer changes its period** (#175). `limit --set` and the dashboard's
  limit button sent `monthly` whatever the key had; without `--reset` a key now keeps its own.
- **A key put under one name is read under the other.** `vault.py put` files a new slot under
  the project's own folder; `use_secret.py run`, `names` and `vault.py list` now read every
  folder that belongs to the project, so older slots and new ones are both found.
- **`vault.py bind --header-for`** accepts an R2 account endpoint (#174).
- **The Mac app:** a Dock click brings the dashboard back when only the Assistant is open (A47).

### Dashboard

- **A Heroku scan that read nothing** says so, with the reason and the re-scan command, instead
  of "0 apps" (A09). The ENV page says why the "Prod" column is empty (A26).
- **Agents:** project names instead of ids, a fetch at once, one clock (UTC), and screen readers
  hear only changes and the pause (A27).
- **Findings:** copied commands run as copied, with this engine's interpreter and a visible
  `REASON`. Titles name the repository. Times are readable. The filter fits at 390 px (A28–A33).
- **Health:** the installed engine and app versions on every path, maintenance warnings once and
  in the reader's language, where the backup passphrase is kept, `backups status`, queue kinds
  as "kind: n", spend in dollars (A14, A34). Long commands wrap (A35).
- **MCP:** when servers were last probed and the command that probes them; a stdio command shown
  once; a missing binary said on its own line (A36, A37).
- **Projects, Domains, Overview, Keys, Machine, Traffic** (A38–A43): a labelled sparkline; one
  group per registrar; a DNS command that asks every record type; translated Cloudflare states;
  one population in the ENV card; locale numbers; the issued-keys ledger shown as a ledger;
  translated disk locations, one size unit and project names on Machine; "owner/.github"
  instead of an id's slug.

### Documentation

- Scenarios: OSS-13's duplicate is OSS-40, stale rows are corrected, every row names its test
  or says it has none, and OSS-41–55 describe fifteen paths that had no scenario (A44, A45).
- ONBOARDING, FORGET, COMPATIBILITY and AGENTS match the code (A23). A receipt for 0.17.0–0.17.3
  (A49). The site says an installation keeps itself current, from signed releases only (A50).
  OBS-19/20 are raised (A51).

Installs at 0.17.x update to 0.18.0 by themselves: their own code checks digests, and from
0.18.0 on every update is also checked for its signature.

## 0.17.3 — 2026-10-05

A patch release. It fixes the root cause behind the torn database copies of 0.17.0–0.17.2,
found by the live verification on a maintainer's machine.

### Fixed

- **A copy of the store could be torn by any process writing to it** (OBS-37).
  - The store's readers opened it `immutable` whenever its WAL was empty. Writers empty the
    WAL at every checkpoint, so an empty WAL says nothing about whether someone is writing.
    An immutable reader takes no locks.
  - In an experiment, 12 copies out of 12 taken under a concurrent writer were torn. With
    locks, 0 out of 12 were.
  - On the maintainer's machine this refused the daily backup and the pre-update snapshot of
    the first automatic update, though the live store was intact.
  - A store is now read `immutable` only when no connection holds it (no `-wal` and no
    `-shm`). Otherwise the reader takes the locks, which creates no file.
- **An update refused for a moment waited a day.** The reason `full update` gives in its JSON
  is now recorded. A transient one is retried the next hour: a torn or changed copy, a busy
  lock, another update, a timeout.

Installs at 0.17.0–0.17.2 take their pre-update snapshot with their own code, so on a busy
machine their first automatic update can be refused a few times before one copy goes through.
Nothing is changed by a refusal.

## 0.17.2 — 2026-10-05

A patch release from the live verification of 0.17.0 and 0.17.1 on a maintainer's machine.

### Fixed

- **The maintenance job was throttled into timeouts.** It ran as a launchd `Background`
  process with low-priority I/O. On a loaded machine (load average above 300) a
  `full update --check` that takes 2 seconds by hand passed its 600-second limit. The job now
  runs as a `Standard` process at nice 10. It is short and hourly.
- **A check that could not look waited a whole day.** An undetermined check (network, rate
  limit, timeout) is now tried again the next hour.
- **A database that is not the workspace's own reached its backups root.** `backup_store`
  exported whatever `OBSERVATORY_DB` named under the workspace's label, so three tiny copies of
  another database pushed the real daily copies out of the rotation. The full snapshots kept
  the data. A database outside `<home>/store` is now copied beside itself only.

## 0.17.1 — 2026-10-05

A patch release. The first live run of the 0.17.0 maintenance job on a maintainer's machine
failed while taking the daily backup. The live database was intact.

### Changed

- **OpenRouter credits Project Observatory's public page.** Model and embedding calls to OpenRouter
  now name `https://passioncode.ai/observatory/` as the app (`HTTP-Referer`), title it
  `X-OpenRouter-Title: Project Observatory` and file it under `programming-app,cli-agent`, so every
  install adds to one public listing. Before, the referer was the GitHub repository, and its app keeps
  its history. A request to any other provider carries none of these headers
  (`agent/providers.py` `attribution_headers`; organization roadmap RM-16).

### Fixed

- **A database copy torn by a concurrent writer crashed the maintenance pass.** Session MCP
  servers keep writing to the store while the tick and server are stopped. A reader opened
  `immutable` (when the WAL is empty) does not see such a write coming, so the copy failed
  its integrity check with "database disk image is malformed".
  - `copy_database` now makes a torn copy again, up to three times. The copy is still
    verified before it counts, and the source is never touched.
  - The daily backup retries the whole snapshot on a database error.
  - Any other error in the backup is recorded in the job's state; the pass completes and the
    tick and server are started again.

## 0.17.0 — 2026-10-05

A minor release, and it **changes behaviour**. Each installation now keeps itself current and
keeps its data where a deletion or a reinstall cannot reach it. Nothing needs to be run after
this release. The companion plugin `observatory-log` moves to 0.17.0: its SessionStart hook
schedules the maintenance job and, for engines before 0.17.0, installs the newer release
itself. Its skills are unchanged.

**Behaviour that changes on upgrade:**
- A new hourly job is scheduled (launchd on macOS, a systemd user timer on Linux). It installs
  each new stable release, keeps the Mac app current and takes a daily encrypted backup.
  `full auto-update off` keeps releases waiting; `full maintain uninstall` removes the job.
- A backup passphrase is generated when none exists and kept in the OS credential store, so
  backups leave the workspace by default.
- Off macOS the default backups root is beside the workspace instead of inside it.

### Added

- **Updates install themselves**, on by default. One job per workspace runs every hour:
  launchd on macOS, a systemd user timer on Linux. Once a day it checks for a newer stable
  release and installs it with `full update --apply`, the same verified, reversible
  transaction a person runs. A failure is rolled back and retried the next day.
  `full auto-update off` keeps new releases waiting; `full maintain status` says what the
  job last did.
- **The Mac app follows the engine.** The job replaces an older app with the engine's
  release once the app is not running. The new app must pass the GitHub digest,
  `SHA256SUMS`, `codesign`, the same signing team and Gatekeeper. The replaced bundle is
  kept.
- **Installs from 0.7.0 to 0.16.0 are reached too.** The `observatory-log` plugin, which the
  PassionCode launcher keeps current, runs such an engine's own `full update --apply` once a
  day when a newer release exists, unless `updates.auto` is `false`.
- **A full encrypted backup every day**, taken by the same job. It covers settings, the
  registry, the vault and the connections, not only the database.
- **A backup passphrase by default, kept outside the workspace.** On macOS it is in the login
  Keychain. On Linux it is in the Secret Service, or in an owner-only file under
  `~/.config`. So a deleted workspace or a reinstall can still open its backups.
- **A reinstall restores.** `full init` in a terminal on an empty home, or
  `full restore --latest`, restores the newest backup a workspace at the same path left.
  `init --fresh` starts empty.
- `doctor` and the Health page show whether updates and backups run by themselves, the last
  check and the newest full backup.

### Changed

- Off macOS, and on macOS without `~/Documents`, the default backups root is
  `${XDG_DATA_HOME:-~/.local/share}/project-observatory-backups`, beside the workspace. It
  used to be `<home>/backups`, inside it, where deleting the workspace deleted the copies.

## 0.16.0 — 2026-10-05

A minor release, and it **changes behaviour**. Agent memory now decides who may reach it, what
may leave the machine, what is current and what can be forgotten:
- every call is authorized against an access binding;
- nothing is embedded remotely without a per-project consent;
- search is scoped before it ranks and leaves a receipt;
- a loopback HTTP service and the `memory/0.1` contract names let agents beyond the local one reach memory;
- facts expire, and lessons cite their evidence;
- the operator can erase a record's text everywhere this machine holds it.

The companion plugin `observatory-log` moves to 0.16.0. Its skills are unchanged and follow
the release.

**Upgrading** runs migration `0010-vector-namespaces`. The pre-namespace vector index is kept
as `legacy` and quarantined, and no new index is activated. Nothing is lost.

**Behaviour that changes on upgrade:**
- An agent's MCP search is lexical only. No remote embedding is made until
  `full embedding-policy grant` gives consent for a project.
- `observatory_recall` returns current records by default. Pass `validity: expired` or
  `validity: all` for history.
- An author can no longer promote its own proposal. The operator's review or an independent
  witness promotes it.
- A new fact (`function: semantic`) must carry `valid_to`.

### Added

- **embedding-policy/1** (PB-137 N-002, N-003, #147, #148, #149, #151): which memory text may
  leave the machine for a remote embedding model.
  - Consent is per project, given at the operator's terminal (`full embedding-policy show|grant|revoke`).
  - It is enforced before the budget, the key and the request.
  - `full doctor` and the Health page show the policy.
  - Confidential text, workflow memory and an agent's query never leave.
- **access-bindings/1** (N-007, N-008, #150, #154, #158): who may do what with agent memory,
  decided from the transport.
  - stdio is the local agent.
  - Any other caller needs a binding the operator issues at a terminal: `full access-binding issue`. The bearer goes only into a new owner-only file.
  - Every tool asks the same gate. Everything outside the memory family is local-only.
  - The target decides the project.
  - `X-Fabric-Projects` narrows a binding and never widens it.
  - Every decision is journalled without the bearer.
- **Search scoped before the window** (N-009, #155): project, class and validity are filtered
  inside the candidate query, so rows a caller may not see neither take a place nor move a
  count.
- **Receipts, explain and redaction on the way out** (N-012, #156, #158):
  - every search names a receipt;
  - `observatory_explain` re-reads exactly the revisions returned;
  - text fields of memory answers and refusal details pass the shape and known-value filters.
- **Checkpoint chunk provenance** (N-011, #157): a checkpoint hit names the body field it
  matched (`decisions[3].why`). Engine ids are not search keys.
- **`full memory-http`** (N-015, N-016, #153, #159): the same MCP server over loopback HTTP
  (127.0.0.1:47313, protocol 2026-07-28).
  - The door checks Host, then Origin, then the binding, then a per-binding rate, before the body is read.
- **memory/0.1 under the contract's names** (N-025, #159): the nine `memory.*` capabilities of
  fabric-agent-contract DEC-0023.
  - They run the same code as the `observatory_*` tools.
  - The schema is vendored at its pinned commit, and unknown fields are refused.
- **`full forget`** (N-013, #161): withdrawal plus erasure of a record's text in the ledger,
  handoff packs, cached answers, indexes, the file and the export.
  - It prints a receipt per backend.
  - Backups and the export's git history are named `retained`, so the receipt is never called
    complete while they exist.
- **Facts and lessons** (N-014, #162):
  - facts carry `valid_to`;
  - recall reads current, expired or all records;
  - `observatory_learn` proposes a lesson from a failure and its fix, citing both.
- **Vector namespaces** (N-005, #152): one index per pinned model identity, with a resumable
  backfill that never activates on its own. No local model is admitted yet (finding F-016;
  OBS-35 is the operator's decision).

### Fixed

- **A late outbox replay brought back the text of an older revision** into the lexical index
  (#157).
- **`handoff_accept` could answer with the previous checkpoint's credentials** when the
  executor wrote a step during the acceptance. The race was observed and is closed:
  `DeclarationMoved` (#154).
- **Six defects found by an independent review** (#158):
  - refusal codes told a foreign workflow from a missing one;
  - search showed checkpoint text without its scope;
  - evidence was not redacted;
  - an owner was named above the class ceiling;
  - an HTTP process could fall back to the local agent;
  - a CLI failure was reported over a binding that had been recorded.
- **A workflow continued while access to a declared credential was unknown** (#145).
- **The OpenRouter door read only the first page of keys** (#144).
- **`survey` raised `KeyError` on a registry row without `ownership`** (OBS-24). Such a row is
  now left out and named in `degraded`.
- **`full update` was refused by GitHub's anonymous rate limit** on a busy machine (OBS-31).
  - It now reads with `GH_TOKEN`, `GITHUB_TOKEN` or `gh auth token` when one is available.
  - The token is sent only to api.github.com and never follows a redirect.

- **A second independent review before the release** found these, and each fix landed with a
  test that is red without it:
  - `full forget` left the erased text in a handoff body's goal, its search chunks, provenance
    and conflict links, and in old full-text segments. It now erases them all, checks the
    store for whole texts and for the record's own distinctive words, says `unverified`
    rather than claiming success when a rerun has nothing left to check, and journals an
    interrupted erasure as one.
  - A fact the operator had confirmed was rewritten as an agent's proposal by the daily
    estate-history run. `ledger.renew` now extends only `valid_to`, keeping the operator's
    state, owner and statement.
  - Validity times in a form that compares wrongly as text (basic format, week dates, a bare
    date) were accepted. One extended form is now required.
  - An unauthenticated flood over HTTP wrote one journal line per request. Refusals are now
    rate-limited per peer before the binding is resolved.
  - `memory.*` input refusals were not marked `isError`, and `memory.record` stored evidence
    strings in a shape the rest of the engine does not read.
- **The Cloudflare door left a freshly minted token live** when its probe or the vault
  refused it (#95). A token the call created is now deleted again, and a refusal says what
  became of it.
- **`tools/update_inventory.py` wrote a path twice while a conflict was open** and `--check`
  accepted it (#97). It now refuses during a conflict and `--check` fails on a duplicate.

## 0.15.0 — 2026-10-04

A minor release: agent memory grows into something agents and the operator use every day —
credentials only through Observatory, recovery after a lost session, the Agents page, and a
search that answers in either language's word forms and says when it found nothing — plus the
header door for HTTP MCP servers. The companion plugin `observatory-log` moves to 0.15.0 (the
`handling-secrets` skill gained `use_secret header` and `vault.py bind`).

Upgrading runs migration `0009-search-stems`, which rebuilds the lexical search index from the
ledger with search keys; nothing is lost, and the index is a projection. The `[full]` extra adds
`snowballstemmer` 3.1.1 (BSD-3-Clause); `full update` installs it from the lock.

### Added

- **A vault bearer for an HTTP MCP server, through Claude Code's `headersHelper`.**
  `tools/use_secret.py header [--env ENV] [--name Authorization] [--scheme Bearer] PROJECT NAME`
  prints one JSON header object, `{"Authorization": "Bearer <value>"}`, which Claude Code puts
  into the request on each new connection. It reads the vault only and refuses, printing nothing,
  when stdout is a terminal, when `CLAUDE_CODE_MCP_SERVER_URL` is unset or names a scheme, host or
  port other than the slot's binding, when the slot is not bound, when `--name` or `--scheme` is
  not an RFC 7230 token, or when the value carries a control character. Every call, served or
  refused, is a row in the `secret-use.jsonl` audit, without the value.
- **`tools/vault.py bind PROJECT ENV NAME --header-for <https URL or host>`** records, in the
  slot's metadata and never with its value, the one server the header door may serve it to;
  `--clear` removes it. Only https is accepted (a bare host means https on the default port).
  Both are journalled in the movements journal, and `vault.py list` shows the binding.
- **Agents get every credential from Observatory, by name.** A workflow declares the keys it
  needs (`credentials` in a checkpoint); the handoff pack says which are in the vault, only in an
  env file, or missing. `use_secret.py run --vault-only` (or `OBSERVATORY_VAULT_ONLY=1`) refuses
  a `.env` fallback; `use_secret.py serve` starts a long-running service with its keys and
  records it as the key's consumer; `vault.py rotate` names the consumers to restart. Values an
  agent writes into memory are replaced by the slot's name before they are stored and journalled
  (`memory-redactions.jsonl`). Design: `docs/design/AGENT-SECRETS.md`.
- **Findings for an agent's keys:** `secret.seen_in_agent_memory`, `agent.secret_outside_vault`
  and `agent.secret_fallback_used`; the leak scan reads the Observatory store too.
- **Agents find and recover their work.** `observatory_workflow_list` lists a project's
  workflows with their stalls and last session; a session that lost its lease token may hand
  the workflow to itself after the silence rule; `full workflow list|show|handoff|close` is the
  operator's command. A step written without the lease is kept as a proposal, not lost.
- **Sessions linked to workflows.** The Stop hook records the workflow a session worked on, and
  a workflow silent for 30 minutes reads as stalled.
- **The Agents page.** Live counters, what needs you, workflows by project, a lane per executor
  with its handoffs, and sessions — refreshed every 15 seconds, in English and Russian.
- **Search in word forms, by checkpoint body, with an honest "nothing found".** Russian and
  English words are matched by their stems, a checkpoint is found by its decisions, results,
  constraints and notes, and a hit must carry a share of the question's subject words; when none
  does, the answer says `abstain` with its reason. A record is searchable the moment it is
  written. Measured on the evaluation set (`tools/memory_eval.py`): recall@5 0.975 → 1.0,
  unanswerable questions answered empty 5 → 20 of 20. The vector arm's own floor comes with local
  embeddings.

### Fixed

- **Redaction of a long note no longer takes an hour.** Two credential-shape patterns were
  quadratic in their input (about an hour on a 2 MB note); they are linear, and
  `observatory_record` checks its length bound before redacting.

## 0.14.0 — 2026-10-03

A minor release: the organisation's product lifecycle contract applied to the engine (#125,
#131), agent memory for workflows and its hardening (#127, #130), and releases built, signed and
published in CI.
Behaviour changes: the scheduled tick no longer probes MCP servers, and background disk sizing
no longer opens privacy-guarded places. After updating, run
`python "$(project-observatory full-path)/tools/install_launchd.py" install` and
`python "$(project-observatory full-path)/tools/serverd.py" --install` once, so the background
jobs pick up the new plists. The companion plugin `observatory-log` is unchanged (0.14.0).

### Changed

- **The scheduled tick starts no MCP server.** It reads the agents' MCP configs only.
  `claude mcp list` runs on request (`full scan-mcp`, `machine.mcp.refresh`) in a process group
  that is killed when it answers or times out, so no server it started outlives it and no
  background job refreshes your Claude login. The last verdict is carried with its time
  (`liveness_at`).
- **Background disk sizing skips privacy-guarded places** — Documents, Downloads, Desktop, the
  media folders, iCloud Drive and other apps' containers. `full machine --disk` sizes them.
- **The server idles.** It re-reads its inputs only when they change, writes its heartbeat on
  change or every five minutes, and slows its beat to two minutes when no client asks.
  `/health` carries `silent_after_s`.
- **Background jobs carry a minimal `PATH`** (the directories holding the tools the engine
  calls, then the system ones) and name the interpreter by its virtual-environment or Homebrew
  `opt` path; the installers refuse a plist naming a versioned Homebrew `Cellar` path.

### Added

- **The tick is bounded.** Every step runs in its own process group under a wall-clock limit,
  the whole tick stops at a 25-minute ceiling below its interval, and
  `store/raw/tick-run.json` records each run's start, end, outcome and reason. The tick plist's
  `ExitTimeOut` is 30 seconds.
- **One log policy** for every log the engine writes: five generations of 5 MB, mode 0600.
- **`stale-server`.** An MCP server whose code an update replaced answers every call with
  `stale-server` instead of running the previous release; `full update` reports the session
  servers it could not reach.
- **The lifecycle watch** (`lifecycle.*` findings): orphaned product processes, session servers
  on replaced code, jobs past their interval, and logs past their cap or readable by others,
  each reported against the product that owns it.
- **Retention owns the pre-upgrade database copies** in `store/migration-backups/`: the newest
  two and any younger than 30 days stay.
- **Agent memory for workflows.** A checkpoint after every step, one executor per workflow by
  lease token, and an immutable handoff pack, so a workflow continues on another account, model
  or session when the one that leaves cannot answer. Five MCP tools
  (`observatory_checkpoint_write`, `_checkpoint_latest`, `observatory_handoff_create`,
  `_accept`, `_get`); migration `0008-agent-memory-workflows`; design in
  `docs/design/AGENT-MEMORY.md`.
- **Agent memory, hardened (#130).** Twelve findings of an adversarial review fixed: checkpoints
  and handoff packs stay out of recall, notes and the observer prompt; search serves only a
  record's latest revision; workflows never cross projects; a handoff needs the lease token (or a
  limit, crash or restart after two silent minutes) and acceptance is bound to the accepting
  session; identifier fields refuse credential shapes; the committed ledger export carries
  workflow rows without their body; an open workflow's checkpoint cannot be tombstoned; a
  credential store is never read into a pack. Details in
  `docs/runs/2026-10-03-agent-memory-hardening/README.md`.
- **Builds clean up after themselves.** `tools/prune_builds.py` keeps the current and previous
  release in `dist/`; `macos/scripts/build-app.sh` runs it.

- **A notarized Mac app per release.** `macos/scripts/notarize.sh` notarizes the
  Developer ID-signed bundle with an App Store Connect API key or a `notarytool` Keychain
  profile, staples the ticket, assesses it with `spctl` and packages
  `ProjectObservatory-<version>-macos.zip`, which the release attaches and lists in
  `SHA256SUMS`. It refuses before uploading when credentials are missing or the bundle is ad hoc
  or lacks the hardened runtime. Apple's log is printed on a rejection. The key never reaches
  the output. 0.13.0 carries the first such download.
- **Releases are built, signed and published in CI.** `.github/workflows/release.yml` runs on a
  `vX.Y.Z` tag. It builds the wheel and the Mac app, signs the app with the organization's CI
  Developer ID, notarizes and staples it, attests every file (Sigstore), and publishes the
  release with `SHA256SUMS` and a GPG signature, `SHA256SUMS.asc`. Nothing runs until a person
  from `release-approvers` approves it; that may be whoever pushed the tag, and an agent never
  approves. A published release is never rewritten. To rehearse, push an `-rc` tag and dispatch the workflow with `publish=false`.

## 0.13.0 — 2026-10-03

A minor release: three audit-and-fix runs over 0.12.0 (2026-10-03) — 173 defects found, 167 fixed (two
of them in part), the rest open and named. The findings, the checks and what is still open are in
`docs/reports/2026-10-03-observatory-audit-run-{1,2,3}/README.md`. The companion plugin
`observatory-log` is 0.14.0: its hook and its `handling-secrets` skill text changed.

### Changed

- **Every engine git call goes through one hardened door** (`safe_git.py`): no credential helper,
  askpass, pager, fsmonitor, hook, signer, external diff, textconv or filter driver runs, only the
  https, http, ssh, git and file transports are allowed, and ssh runs in BatchMode. Plain values
  of your git config (`url.*.insteadOf`, `safe.directory`, excludes, proxies) still apply. A
  consequence: a collector sees an LFS file as its pointer. SECURITY.md lists what remains.
- **The wheel carries the tested dependency lock**; README → Install is two steps (the wheel, then
  its `[full]` extra under that lock), and `full update --apply` installs with it.
- **A vault `PROJECT` is the project's folder name**; a registry id (`project:local-alpha-web`) or
  name is accepted and normalised to that folder, and two projects claiming one string are refused.
- **`full agent install` checks that Claude Code really installed the plugin** before saying so,
  and `uninstall` leaves `settings.json` as it was before the install.
- **No key is not "ready"**: `model_status` is `no-key`, `next` names `install_key.py`, and doctor
  and `assistant status` show where the key comes from (an inherited `OPENROUTER_API_KEY` too).
- **Bare `observatory` without a workspace exits 2** (it printed its guidance and exited 0).
- **The registry guard** refuses a swing over ±25% only above a floor (more than 5 projects added
  or more than 1 lost), so a small estate can grow; an empty registry is always refused.
- **Every `full <command> --help` prints help and runs nothing**, `full google --force` and
  `full plugins --only ID --force` are accepted, and doctor lists every known setting.
- **PassionCode design system 1.1.0**: the dark control edge is `#6f5e77` (3:1 on every surface),
  on the dashboard, the website and the Mac app.
- **The Mac app**: one dashboard window; a window reopened after a workspace change shows that
  workspace; Start Server is offered only where it can start; no model or budget is a named step;
  the assistant and Settings in the dashboard's tones; ⌥⌘↑/↓ walk the conversations.
- `configure model chain` and `configure budget` set what the assistant needs; `vault.py remove`
  removes a slot or its retired archives on the record.

### Fixed

- **Keychain and prompts:** `git ls-remote`, scheduled commits, the remotes probe (a `gcrypt::`
  remote started its helper), collectors and the Stop hook could run a credential helper, signer,
  hook or filter of the user's git config; the dashboard window cancels every password or
  certificate challenge.
- **Lost work:** `full cleanup --apply --include manual` could remove a dirty worktree after saving
  an empty or unappliable patch; it now proves the patch applies first.
- **Keys:** a second sighting of one leaked slot stopped the whole board; a value typed as a name,
  owner or note reached the registry and the pages; the keyserver's leak route could be turned into
  vault options; refusals over MCP and the keyserver quoted the caller's value; a vault slot
  reached the Keys page only after an OpenRouter scan; a key with no limit read as spent.
- **New user:** `full local` with no projects source ended in a traceback; the first board said the
  dashboard was unverified; a folder whose remote is on another host had no history and its
  unpushed commits were reported nowhere; seven handed-over commands were refused by the step
  runner; messages named source-tree paths; a missing `[full]` extra was blamed on the interpreter.
- **Dashboard:** keyboard focus was lost after a sort or a reset; the project panel said "no
  commits" for a project with commits; empty pages gave no command; demo pages carried the
  builder's paths; the server answered HEAD with 501; the Russian copy had meaning errors.
- **CLI:** a bare `full open --stop` looked on the default port, not this workspace's.

## 0.12.0 — 2026-10-02

A minor release: the Mac app opens on the dashboard, has its icon, always comes back to a window,
and what it starts can be stopped again.

### Changed

- **The app opens on the dashboard.** Its main window is the workspace's dashboard: live when the
  workspace's own server is verified on 127.0.0.1, the built pages from disk when it is not (they
  are self-contained), under a banner with **Start server**; an unbuilt workspace offers **Build
  the dashboard**. Navigation stays inside this workspace's pages and everything else opens in the
  browser; the page's confirm and prompt are native sheets; the app's language and the page's
  follow each other; the window's chrome is dark like the pages; rebuilt pages reload in place.
  The assistant moved to its own window (⇧⌘A).
- `project-observatory full assistant dashboard` names the server state (`verified`, `absent`,
  `other-workspace`) and the built pages; `serve` and `build` are the explicit actions behind the
  app's buttons (an installed always-on server is restarted through its launchd job).

### Fixed

- **What the app started could not be stopped.** posix_spawn hands the calling thread's signal
  mask to the child and a Swift worker thread blocks the asynchronous signals, so every process the
  0.11.0 app started inherited a blocked SIGTERM: a server it started ignored `full open --stop`,
  and an assistant runner kept waiting on the model after Stop. The app now spawns with a clean
  mask, and `serverd` and `run_job` unblock their stop signals whoever starts them.
- **No window after closing it.** Clicking the Dock icon, or launching again, did not recreate the
  closed main window; the app now always ends with the dashboard on screen.
- **No icon.** The app shows the product mark, rasterized on the macOS icon grid at every size.
- `macos/scripts/install-app.sh` installs the app and forgets Launch Services registrations of the
  same bundle left by builds elsewhere, which Spotlight could open instead.
- An engine that answers the dashboard action in the 0.11.0 shape is named incompatible instead of
  being shown as a workspace without a dashboard.
- **Finding titles read in Russian.** Every finding carries a message id and its arguments
  (`title_id`, `title_args`) beside the English `title`, which is rendered from them; the
  dashboard shows the title in the reader's language with correct plural forms, and a test fails
  when a rule's title has no id or no translation. Several English titles change wording: "(s)" is
  replaced by the proper singular or plural, and two titles were reworded so a second count needs no
  agreement. Anything matching on the old title text sees the new wording. Details and actions stay
  as their rules write them.
- An estate with no scan yet no longer reads "Measured not measured" ("Измерено не измерено") in
  every page's header.

## 0.11.0 — 2026-10-02

A minor release: the native Mac app and its shared assistant, and an agent channel an agent can
actually read. Behaviour changes for MCP callers are listed under **Changed**.

### Added

- **Project Observatory for macOS** (`macos/`, `docs/macos/`): a SwiftUI window that asks the
  workspace's configured model about bounded local evidence and shows the facts each answer used.
  `project-observatory full assistant status|ask|get|job|cancel|delete|list|dashboard` is the same
  workflow for the CLI, and `observatory_assistant_status`, `observatory_assistant_ask` and
  `observatory_assistant_conversation` for MCP hosts (#112, #114). An engine without the assistant
  is reported by the app as incompatible, with the update command.
- `observatory_findings` takes `projectId`, `limit` and `cursor`; `observatory_machine` takes
  `section`; `observatory_assistant_status` takes `includeProjects`.

### Changed

- **`observatory_overview`**, the agent's entry point: counts, activity tiers, the most recently
  active projects, the most severe findings and this machine's disk, about 5 KB on an estate whose
  unpaged survey is 165 KB. `observatory_status` keeps the published "no limit means everything"
  (the v0.2.0 probes rely on it) and gains `detail: "summary"` (about 400 characters a project) and
  `detail: "full"`; by default every row now carries only the published survey fields, and the
  organization and recorded resources come with `detail: "full"`.
- `observatory_machine` answers an overview with the top ten of each section; a named `section`
  in full; `explainPid` alone with its explanation. It was 168,000 characters every time.
- Smaller defaults: `observatory_findings` 20 (most severe first), `observatory_recall` 10,
  `observatory_timeline` 25. Text copies of `observatory_*` answers are compact JSON.
- Server instructions fit under 1,800 characters with the SPENDS, WRITE and SECRETS rules first; a
  host keeps about 2,048 and cut the old text at the WRITE rule.

### Fixed

- **`estate.survey` failed on every call** on an estate that records organizations: survey rows
  carried `organization` and `resources`, which the closed v0.2.0 item schema does not list. The
  capability now meets its published schema and names what it omits; `observatory_status` keeps
  both fields.
- `observatory_credentials` and `observatory_timeline` name an id the registry does not hold in
  `degraded` instead of answering an empty list that reads as clean.
- Findings reach a project through one shared matcher (its repositories and clones, its sites'
  domains, the secrets and env files in its folders), in `project.detail` too.
- A client that disconnects mid-answer no longer leaves a traceback in the server log (#107).
- Dashboard: every subject link lands on a row that is rendered, and a subject without one stays
  text; env links resolve folders to registry ids and an unknown project says so; the domains
  tile, nav badges and pages count the same sets; every grouped table folds; a linked row is no
  longer hidden under the sticky header; finding types are labelled in English and Russian;
  units, plural agreement, enum words, the health queue and the machine page at 900 px are fixed
  (#114). Finding titles, details and actions are still written in English by their rules.
- The `observatory-log` plugin is 0.13.1: an example in `tracking-resources` carried a real
  analytics account id and now carries a synthetic one (#107, #114).

## 0.10.1 — 2026-10-01

A patch release: the always-on server is no longer scheduled as background work, and Fabric
Dashboards is the one notification channel where it is installed.

### Fixed

- **The server is scheduled as a standard process.** Its launchd job carried `ProcessType
  Background`, `Nice 5` and low-priority I/O; under load macOS starved it, and on 2026-10-01 Fabric
  Dashboards recorded it "not answering" 30 times in a day while the process ran without a single
  restart. `tools/serverd.py --install` now writes `ProcessType Standard` with neither; the
  scheduled tick stays background work. Re-run `--install` to apply it to an installed server.
- **One notification channel.** When the server's Fabric descriptor is installed and Fabric
  Dashboards is on the machine, `tools/notify_findings.py` raises no banner of its own: the host
  delivers `finding.opened` from the events feed, titled with the agent and what it wants (Fabric
  Dashboards ADR-0010). Before, the operator got each finding twice. Without the app it notifies
  as before.

## 0.10.0 — 2026-10-01

A minor release: the licence changes, and collectors report sources that do not apply here
without degrading the service.

### Changed

- **Licence.** This release is the first under `AGPL-3.0-only OR
  LicenseRef-PassionCode-Commercial`: open source under the GNU AGPL-3.0, with a commercial
  licence from PassionCode.ai (`LICENSE`, `COMMERCIAL-LICENSE.md`, every manifest and `SKILL.md`).
  v0.8.2–v0.9.1 keep PolyForm Noncommercial or Internal Use, v0.8.1 and earlier MIT (#90, #91).
- `google-auth` 2.58.1, with the lock (#83).

### Added

- Cloudflare door presets, each delivering into a vault slot only after its new token passes
  its own probe: `email-send`, `email-routing` and `workers-edit`, and `cloudflare.py groups
  --account <slug> --match "<words>"`, a read-only listing of permission-group names (#89);
  `r2-bucket`, one bucket's S3 key pair, with the bucket and its lifecycle made by a setup token
  deleted before the command returns (#94); `fabric-inbox-server`, `fabric-inbox-account` and
  `workers-observability-read` (#93).

### Fixed

- **Sources that do not apply here held the service `degraded`.** A collector now puts a
  source it measured as not applicable on a `not_applicable` list beside `degraded`: shown on
  the board as one `collector.not_applicable` info row per receipt, never counted as degraded
  coverage. It covers a TLD whose registry runs no RDAP service (absent from the IANA bootstrap)
  for a domain DNS shows is held; a companion tool that is not installed, told apart from one
  installed without its store or key; a Google credential refused by an API its Cloud project
  never enabled, when another credential reads that surface; and a receipt left behind by an
  integration the workspace has switched off, which was read as current for days (#104).
- **An OpenRouter key was reported gone because a bounded listing did not reach it.** The listing
  reads the newest keys only, and an account where another product mints hundreds a day pushed a
  consumer's key past that bound. Missing keys are now searched for once past the bound and
  remembered by hash; the provisioning key, which no listing includes, is confirmed with
  `GET /key`; a key is reported gone only on a 404 for its hash or a listing walked to its end (#104).
- **A repository whose remote has one branch had "no default branch".** With no `origin/HEAD`
  and no `main` or `master`, the remote's only branch is its default; with several, the reason
  names `git remote set-head origin --auto` (#104).
- **One slow answer from an external command degraded a whole collector.** `git` calls of the
  filesystem scan and `heroku auth:token` are asked again with a longer limit after a timeout
  (`slow_command.py`), and a command that misses every attempt is reported with each duration
  and the load average (#104).
- **A leak-scan suppression could hide a rotated value or another file.** It matched only the
  credential name and a path substring; it is now bound to an exact location and the value's
  identity, malformed or legacy rules are refused with a warning, and evidence is replayed when a
  decision expires (PB-032, #62).
- **A spent key's reset date moved with the clock.** It was counted from today, so a key measured
  as spent in one month read as still blocked in the next; it is now counted from the
  measurement's `checked_at` (#98).
- `test_leak_coverage.py` built its dates from the local clock while the scanner uses UTC, so it
  failed between local and UTC midnight east of Greenwich (#94).

### Companion plugin 0.13.0

- Licence metadata in `plugin.json`, both marketplace entries and every `SKILL.md` front matter
  is `AGPL-3.0-only OR LicenseRef-PassionCode-Commercial`; 0.12.3 shipped the PolyForm text.
- `handling-secrets` documents the new Cloudflare presets (`r2-bucket`, `email-send`,
  `email-routing`, `workers-edit`, `fabric-inbox-server`, `fabric-inbox-account`,
  `workers-observability-read`) and the read-only `cloudflare.py groups` listing.

## 0.9.1 — 2026-09-30

### Fixed

- **Claude Code could not list any of the MCP server's tools in 0.9.0.** It refuses a whole
  `tools/list` answer ("tools fetch failed — Handler returned an invalid result") when one
  tool's `outputSchema` is not an object schema at its root, and two were rooted in `oneOf`:
  `project.record`'s published v0.2.0 output schema and the job-tool union of
  `machine.mcp.refresh`. Both are now served with `"type": "object"` added at the root; every
  branch of both is an object, so they accept exactly what they did. A test holds every served
  tool to an object-rooted input and output schema. Restart Claude Code sessions after the
  update.

## 0.9.0 — 2026-09-30

### Added

- **`machine.mcp.inventory`**: every MCP server the agent configs on this machine declare —
  Claude Code (user and project scopes, plugins, claude.ai connectors), Cursor, OpenCode,
  Codex, Gemini CLI and Kiro — by name, with the configs that declare it, its transport
  (`stdio`, `streamable-http`, `sse`) and whether it answered the last probe, and when. Names
  and verdicts only: no URL, command line, header or environment value is read into it. An
  absent or unreadable config and an inventory older than the tick allows are named in
  `degraded`. Enable it with `full configure sources mcp_config_root "$HOME"` and
  `full configure integrations mcp true` (#84).
- **`fabric-interop/0.1`** on the MCP server: every Fabric capability is also a tool of its
  own name — `estate.survey`, `project.detail`, `project.timeline`, `project.record`,
  `machine.mcp.inventory`, `machine.mcp.refresh` — taking and returning exactly its
  published schemas; `_meta.traceparent` continues as a child span; `machine.mcp.refresh`
  takes a new inventory as a job followed with `fabric.job.get` and stopped with
  `fabric.job.cancel`, whose result is the contract's full result envelope with its trace.
  Built against fabric-agent-contract `9cd778e` (DEC-0017) and fabric-agent-adapter 0.5.0,
  whose kit and conformance probe are vendored. The ten `observatory_*` tools are
  unchanged (#85). Design and limits: `docs/design/FABRIC-INTEROP.md`.
- `tools/serverd.py --install` writes a per-install manifest (this interpreter, the server,
  the workspace) and the service descriptor points at it; the server keeps it current after
  an update. `mcp/server.py` accepts `--home PATH`.
- The README quick start registers the MCP server with Claude Code (`claude mcp add …`).

### Changed

- The Fabric manifest is revision 7 (profile 1.2.0). The v0.2.0 schema identifiers are
  unchanged; the new schemas are published at `v0.9.0`, and the contract lock pins each file
  to the release that publishes it.

### Fixed

- **A URL's `user:password@` part reached the MCP scan file.** Only the query string was
  dropped; the user-info now is too, and it counts as a key in the URL.
- `observatory_status` skipped the value check for a `scope` object: `{"scope": {"kind":
  "owner"}}` surveyed the whole estate instead of answering `missing value`.
- `publish_contract.py --local-manifest` named the virtual environment's base interpreter
  (resolved through its symbolic link), so the server it described could not import its
  packages.

## 0.8.2 — 2026-09-29

### Changed

- **License.** From 0.8.2 Project Observatory is source-available, not open source. It is
  offered under `PolyForm-Noncommercial-1.0.0 OR LicenseRef-PolyForm-Internal-Use-1.0.0`,
  at the user's choice. Individuals and noncommercial organisations may use, change and
  share it for noncommercial purposes. Any company may use it, and change it, for its own
  internal operations. Distributing it commercially, or building it into a product or
  service provided to others, needs a separate commercial license (contact@passioncode.ai).
  Copyright: Siarhei Sheleh. **Versions up to and including 0.8.1 were released under the
  MIT License and remain available under MIT.** Contributions are accepted under
  [CLA.md](CLA.md).
- Package, plugin and marketplace metadata name PassionCode.ai as author and owner; the
  project homepage is https://passioncode.ai/observatory/. Security and conduct reports go
  through GitHub private vulnerability reporting or contact@passioncode.ai.
- observatory.sshlg.me is no longer a product page: `/` redirects to
  passioncode.ai/observatory/, and the field notes stay where they were.

### Companion plugin 0.12.3

- License and author metadata as above; no behaviour change.

## 0.8.1 — 2026-09-29

### Fixed

- **`full agent` fought a launcher that installs the same plugin.** `status` looked only for
  `observatory-log@observatory-log`, so a copy installed under another marketplace id (the
  PassionCode launcher's `observatory-log@passioncode`) read as "plugin not installed", and
  `install` added a second copy: every hook fired twice until the launcher removed it again.
  Any installed and enabled `observatory-log@<marketplace>` is now the plugin. `status` is ok
  and names the managing channel, `install` writes only the hook environment
  (`OBSERVATORY_ROOT`, `OBSERVATORY_HOME`, `OBSERVATORY_PYTHON`) and adds no second id,
  `uninstall` keeps the environment that copy still reads, and two enabled copies are
  reported with the command that removes one. `full update` suggests `full agent install`
  only when the engine's own channel manages the plugin.
- **`full doctor` was silent when a configured source had been deleted.** The `sessions`
  integration reads the companion database, but `companion_db` was not among its needs, so a
  deleted database left `coverage_warnings: []` while every tick logged DEGRADED. A missing
  path, or a directory where the database file should be, is now a warning, and every warning
  carries `fix` (`full configure sources NAME PATH`) and `disable`
  (`full configure integrations|features NAME false`).
- **`scan-mcp` timed out on machines with many MCP servers.** `claude mcp list` health-checks
  each server before printing. The limit is now 180 s (was 120), `OBSERVATORY_MCP_PROBE_TIMEOUT`
  raises it (1–3600 s), and `install_launchd.py` carries it into the tick's plist. A probe that
  still runs out of time is degraded: servers reported before the cut keep their verdict, the
  rest are `not-probed`.
- The dashboard builder no longer uses the deprecated `datetime.utcnow()`.

### Companion plugin 0.12.2

- `tracking-resources` no longer triggers on creation requests ("new Figma file", "add
  analytics"). It is the record-after step, and names the skills that create or wire instead.
- `plugin.json` points at a schema that resolves (`claude-code-plugin-manifest.json`).
- The engine's plugin suite checks every shipped SKILL.md front matter as strict YAML. An
  unquoted `: ` in a description, which `claude plugin validate --strict` accepts, now fails it.

### CI

- `claude plugin validate --strict` runs on the root marketplace, the engine marketplace and
  the plugin (Claude Code pinned; ubuntu-latest / Python 3.14 row of the required `test` job).

## 0.8.0 — 2026-09-29

### Added

- **The always-on server speaks `fabric-service/0.1`**, the Fabric Agent Contract's local service
  extension, so a host such as Fabric Dashboards can find and watch it without knowing it in
  advance ([design](docs/design/FABRIC-SERVICE.md)):
  - `GET /.well-known/fabric-service` — who answers (`project-observatory`, instance `default` or
    `ws-<sha16>` per workspace), which build (commit or package digest), pid and start time,
    `status` and `degraded` from the collectors and the tick verdict, and four tiles: projects,
    open findings, open leaks, last tick. Answered from memory; `/health` is unchanged.
  - `GET /fabric/v1/events?after=&limit=` — the event store as sentences with a cursor: grouped
    commits ("3 commits in Fabric by …"), agent sessions, findings opened (`notify: true`) and
    resolved, each linking to its project or finding. Requires the workspace's new
    `service.token` (mode 600) as `Authorization: Bearer`.
  - **One copy per workspace.** `serverd.py --run` locks `service.lock` in the workspace before
    anything else; a second copy exits 75 naming the holder. `full open --serve` on another port
    now says which address already serves the workspace.
  - `serverd.py --install` writes the service descriptor (claiming the port, refusing one another
    service declares) before the plist, uses bootout-wait-bootstrap with retries and waits until
    the server answers as itself; the plist gains `ThrottleInterval`, `ExitTimeOut` and
    `ProcessType`. `--uninstall` removes the descriptor too. SIGTERM now drains and exits 0.
  - `fabric-agent.json` revision 5 names the descriptor under the extension key.

### Fixed

- **Both servers bind without a reverse DNS lookup.** `http.server`'s `server_bind()` calls
  `socket.getfqdn()` between `bind()` and `listen()`; with a slow resolver (the macOS CI runner)
  the port stayed bound but silent for longer than 20 s, so a probe neither connected nor was
  refused. `serverd` and the portable `observatory` dashboard now name themselves `127.0.0.1`.
- **A failed heartbeat no longer leaves the server `starting` forever.** A full disk or a failing
  collector becomes a `heartbeat` or `health` row in `degraded`, and the snapshot is still
  refreshed.

## 0.7.1 — 2026-09-28

### Fixed

- **`full profile export` carried no model chain from a real workspace.** A policy file
  was excluded whole when any value held a machine path, and the operator's `models.json`
  had one in two prose fields (`wallet.source_note`, `embedding.contract_source`). The
  second machine then got an empty chain and zero spending ceilings, and its agent layer
  was off without saying so. Now, when only documentation fields (keys ending in `_note`
  or `_source` with a string value, which the engine never reads) hold a path or a token,
  those fields are dropped and listed in `excluded`, and the file travels. A path in any
  other field still excludes the file.

## 0.7.0 — 2026-09-28

A second machine can now run exactly the same Observatory as the first and stay in step with it.
Every install is a tagged release of this repository; the engine code that still lived only in
the private predecessor has come across.

### Added

- **`full update`** installs a GitHub release with no manual steps. `--check` exits 0 (up to
  date), 10 (a newer release) or 3 (could not look: never reported as up to date). `--apply`
  installs the wheel only when it matches both the release's `SHA256SUMS` line and GitHub's own
  asset digest. It then stops this workspace's launchd jobs, snapshots the workspace, installs
  with the running interpreter (the `[full]` extra included), runs the new release's `upgrade
  --apply` in a fresh process, checks version, pins and `doctor`, and restarts only the jobs it
  stopped. On failure it reinstalls the previous verified wheel, which is kept under
  `backups/engine-releases/`, and restores the snapshot. It refuses downgrades, source
  checkouts and editable installs. Steps are logged to `store/logs/update.jsonl`.
- **`full profile export | import`** moves the functional configuration between machines:
  integrations, features (except the scheduler), interface language, the model chain and the
  machine-independent policy files. It never carries sources, paths, secrets, registry, store,
  account ids or credential annotations, and a value shaped like a path, email or token is
  refused on both export and import. Import previews by default, applies atomically under the
  workspace lock, refuses a profile from a newer engine, and lists which enabled integrations
  still need sources or logins on this machine.
- **`full open --stop`** ends the dashboard server that `open --serve` started for this
  workspace, and only that one.
- **Cloudflare `dns-edit` preset**: `tools/cloudflare.py issue --preset dns-edit --zone Z --vault
  P/E/N` mints a token scoped to one zone (Zone Read + DNS Write), verifies it against that zone
  and delivers it into a vault slot over stdin. The handling-secrets skill routes to it
  (`observatory-log` 0.12.1).
- **The historical engine suites.** 130 suites exported with the engine on 2026-09-23 but left
  behind now run in `full check`, adapted to synthetic workspaces: 192 suites, up from 62.
  `test_step_references` fails if the step list names a file that does not exist (124 of 206
  did) or a suite the portable runner does not run.
- `AGENTS.md` for agents, with `CLAUDE.md` importing it, and a link to the organisation map.

### Fixed

- Operator messages named `collectors/<file>`; curated files live in the workspace's `config/`.
- keyserver audit rows now state that the caller is a token holder, not a verified identity.
- Trap T31 mutates the merge rule that decides whether a remote-less checkout is dropped.
- `mcp.own_unregistered` suggests a registration command that runs as written.
- Findings had no remedy for the merge's `github`, `wiki` and `identity` degradations.
- `skill_check` kept YAML quotes around the shipped version, so it never reported OK.
- `trace_opens` seeded its sandbox from the source tree instead of the workspace.
- Plugin `secret:NAME` readers and the default meaning of an unknown activity tier.
- `dashboard/audit_pack.py` could not import `paths` when run as a script.
- `review accept-proposal` named a checkout-relative file instead of the one it wrote.
- `skip_sites.survey()` crashed on a directory outside the program tree.
- `registry_shape` described `degraded` notices instead of records on a small estate.
- The dashboard purity allowlist was missing the machine page added in 0.6.0.
- README selects a supported interpreter explicitly; stock macOS `python3` (3.9) stops with a
  message instead of a misleading resolver error. Every ONBOARDING command runs as written.
- CODEOWNERS named an organisation, which GitHub rejects; it names the `contributors` team.

## 0.6.3 — 2026-09-28

### Added

- **`observatory`**, the short name: the package installs it beside `project-observatory`, same
  commands.
- **With no arguments, either name opens the dashboard** of the complete engine's workspace
  (`OBSERVATORY_HOME`, or the default one; `--home PATH` selects another) — `full open`, which
  builds the pages if needed. With no workspace it prints where to start (`full init`, `demo`)
  instead of an argument error. Every existing command parses as before.

## 0.6.2 — 2026-09-27

### Added

- **Swap is a disk row.** On macOS the swap files live on the VM volume, which shares the APFS
  container with the data volume, so memory pressure shrinks free disk. The machine survey now
  measures them (one `stat` per file; `/proc/swaps` on Linux) as "Swap files (memory written to
  disk)" on the Machine page, and `machine.disk_low` says how much of the shortfall is swap. Found
  the evening 0.6.0 shipped: 15 GB of swap was most of an 11 GB fall in free space that no measured
  place explained.

## 0.6.1 — 2026-09-27

### Fixed

- **The machine survey no longer holds the tick.** Disk places are sized within a time budget per run
  (`disk_budget_seconds`, 90 by default; `disk_location_timeout_seconds` per place), oldest
  measurement first; a place not reached keeps its last number and its own `measured_at`, and one
  that does not fit is reported, never shown as zero. `full machine --disk` uses
  `disk_budget_seconds_manual`. Measured on the machine that found it: the first 0.6.0 survey held a
  tick for 23 minutes under memory pressure; 0.6.1 sizes the same 19 places in 44 seconds.
- `du` runs with `-x`: a mounted simulator image or VM volume is no longer counted as the host disk's
  usage, and a timed-out `du` is no longer run a second time.

## 0.6.0 — 2026-09-27

### Added

- **The Machine page and `full machine`.** Processes grouped by origin — agent session, launchd job,
  simulator device, application, system, detached — with memory, CPU and project attribution; memory
  and swap; free disk and the largest cache, simulator, VM and history locations from
  `config/machine.json`, each with how its space comes back. `full machine --explain PID` says why a
  process runs, using witr when it is installed. A process is never stored with its environment or full
  command line.
- **Git hygiene**: every registered checkout's worktrees and branches, classified (merged, patch-merged,
  pushed, unique; clean, dirty, missing, in use, idle days).
- **Cleanup.** `features.auto_cleanup` removes on each tick only what loses nothing — merged or pushed
  branches, clean idle worktrees, stale worktree records, build output of idle projects — re-checking
  every target when it acts and journalling each removal. `full cleanup --apply --include manual` also
  removes unique branches and dirty worktrees after bundling and archiving them.
- `observatory_machine` (MCP, read-only): the same summary for agents, with `explainPid`.
- Findings: `machine.disk_low`, `machine.memory_pressure`, `machine.heavy_origin`,
  `machine.detached_servers`, `machine.stale`, `git.idle_dirty_worktrees`, `git.idle_unique_branches`,
  `cleanup.done`.
- `features.machine_watch` gates the three new tick steps (`machine`, `git-hygiene`, `cleanup`); both
  features default to off.

## 0.5.0 — 2026-09-27

### Added

- **Organizations.** `config/organizations.json` declares the owners an estate serves, where each
  keeps its Google Analytics properties and Figma files, and how a project is matched to one
  (repository owner, product membership, explicit declaration, `external`, default). Every project
  carries `organization`, `organization_source` and `organization_why`; `observatory_project` adds the
  destination (`ga4Account`, `figmaTeam`, `figmaProject`). Two matching owners is a reported conflict,
  never a guess. An empty file (the new default) changes nothing.
- **Resources.** `observatory_propose` accepts `organization` and `resources` for any project, without
  waiting for an operator to hand-write the first row. A resource has a closed `kind` vocabulary
  (analytics, Firebase/Google Cloud, server, database, DNS zone, cloud/payment/app-store account,
  Figma file, other) and a stable `identifier`; accepting it APPENDS to the project's list.
- **The SessionStart line** names the project's organization, its GA account and Figma team, how many
  resources are recorded, and the duty to report new ones. The `tracking-resources` skill in the
  `observatory-log` plugin (0.12.0) makes that duty explicit.
- Three analytics findings: a property in another organization's account, a new property in a legacy
  account, an organization's account no service account reads.

## 0.4.1 — 2026-09-27

### Added

- **Encrypted backups off this disk.** One backups root for every copy the engine keeps: the daily
  database copy, `workspace-backup` snapshots and the snapshot `upgrade --apply` takes first. On macOS
  it defaults to `~/Documents/Project Observatory/Backups` (iCloud Desktop & Documents carries it off
  the machine); elsewhere to `<home>/backups`. Override with `OBSERVATORY_BACKUPS` or
  `full configure storage backups PATH`. Everything written there is encrypted — AES-256-GCM in
  authenticated chunks, scrypt-derived key — with a passphrase set by `full backup-passphrase set`.
- `full backups status|migrate|decrypt`; `full restore` accepts an encrypted `.obsnap` file, taking the
  passphrase from `OBSERVATORY_BACKUP_PASSPHRASE` or a terminal prompt.
- `storage` in `config/settings.json` (`{"backups": "/absolute/path"}`). Releases before 0.4.1 ignore it.
- `full doctor` reports `backups`: root, the rule that chose it, whether it is encrypted, the newest
  artifact per kind, and warnings — never the passphrase.

### Changed

- **Snapshots rotate.** Three per kind are kept, locally and in the root. Before this release
  `<home>/backups` grew by one snapshot per upgrade and per manual backup, with no limit.
- Without a passphrase nothing leaves the workspace: copies stay where they were, unencrypted, and
  `doctor` warns. A plaintext copy never reaches the backups root.
- The tick asks `tools/backup_store.py --due` whether a daily copy is due, instead of searching one
  directory for one file name; the copy may now live in either place.
- `cryptography` is a declared dependency of the `full` extra (it was already in the lock file).

## 0.4.0 — 2026-09-26

Project Observatory is now a PassionCode.ai product. The repository lives at
[passioncode-ai/project-observatory-dashboard](https://github.com/passioncode-ai/project-observatory-dashboard);
GitHub redirects the previous address, and no workspace migration or key rotation is needed.

### Added

- **The dashboard speaks English and Russian.** English is the default. A workspace chooses its
  language with `project-observatory full configure interface locale ru` (or `en`); `full doctor`
  reports it. Each reader can switch between EN and RU in the navigation rail. The choice is kept in
  the browser, is applied before the first paint and works for pages opened as local files. Every
  interface string is an English message id; `observatory/engine/dashboard/locales/ru.json` holds the
  Russian, with CLDR plural forms (1 проект, 2 проекта, 5 проектов) and locale number grouping. Page
  titles, navigation, tables, filters, counters, confirmations and action outcomes are translated, as
  is the session-start line of the Claude Code plugin. Finding texts written by the finding rules stay
  in English: they are data, not interface.
- `interface` in `config/settings.json` (`{"locale": "en" | "ru"}`). Releases before 0.4.0 ignore it,
  so a workspace that sets it stays readable by them. An unknown key or value is refused.
- `tools/demo_estate.py` builds the real dashboard over a fictional estate for screenshots and demos.
  It reads nothing from the machine that runs it.

### Changed

- **PassionCode design system 1.0.0.** The dashboard and the website use the PassionCode tokens,
  vendored byte for byte and pinned by commit and SHA-256 (`dashboard/brand/manifest.json`): one fixed
  dark theme, gold for action, selection and focus, and a product glyph — an observing lens with a gold
  point — on the family's dark tile. The rail names the family, "by PassionCode.ai". The light/dark
  theme switch is replaced by the language switch.
- Stats in the dashboard payload use stable ids (`commits_7d`, `projects_active_7d`,
  `unpushed_commits`, …) instead of Russian labels; the page supplies the words.
- Metric captions in the shipped plugin manifests are English message ids (`dependencies`,
  `on disk`, `git branches`, …), translated by the page. A third-party plugin's caption reads as its
  author wrote it.
- The store's degradation note reaches the page as a message with arguments, so it is translated too.
- The website shows the real dashboard rendered over the fictional estate, names PassionCode.ai in
  its header, metadata and footer, and lists Observatory beside Switchboard as the PassionCode tools
  available today.
- `full agent install` moves a marketplace that still points at the previous repository address;
  `full agent status` names that case instead of calling it a non-GitHub source. Companion plugin 0.11.2.

### Maintainer tooling

- `tests/test_i18n.py` proves that the dashboard sources carry no Russian, that every message id has a
  translation with matching placeholders and complete plural forms, that the builder's plural rules
  equal `Intl.PluralRules`, that English pages hold no Russian, and that the vendored design tokens
  match their pin. Render harnesses run the page's own language runtime in both languages.

## 0.3.12 — 2026-09-26

### Changed

- The ten-page dashboard has persistent navigation, visible list rows and a compact
  project summary with full linked detail. ENV metadata is visible by default;
  revealing a value remains an explicit protected action.
- Sorting compares all selected rows across accounts and groups. A separate sort
  control remains available on narrow screens; opposite filters replace each other.
- Overview shows a bounded preview across all severities. Findings retain visible
  titles, searchable evidence and acknowledged history even when none remain open.
- Copy-only actions say they copy a command. Hosting cost and traffic coverage sit
  above their lists; Health starts with the observer's measured state.

### Fixed

- Repeated observations of the same GA4 resource through different credentials no
  longer duplicate rows or traffic totals. Provenance is retained, conflicting
  measurements are marked unknown, and cached registries normalize when rendered.
  Totals describe sums across properties, not unique people across products.
- Unknown traffic no longer matches the measured-zero filter. Findings search uses
  its own keyboard shortcut, and detail links restore focus when closed.

### Maintainer tooling

- Source checks parse each file once per metric-vocabulary guard. Private-name
  checks factor common prefixes while preserving longest-first boundary matching;
  a reference-matcher regression covers case, punctuation and Unicode.

## 0.3.11 — 2026-09-24

### Fixed

- The leak scan counted a file it could not open as read and clean. An unreadable file, a transcript
  whose `stat` failed, and an engine that isn't a git checkout are now listed as unread, and an
  unreadable file raises a warning. The report gains a `coverage` block.

### Added

- Leak suppressions with a reason and an expiry (`config/leak_suppressions.json`). A suppressed
  sighting is still shown along with its reason. An expired or incomplete rule is not applied and is
  reported.

## 0.3.10 — 2026-09-24

### Fixed

- A dashboard action whose answer was lost said "не вышло" (failed) and let you try again, even
  though the provider may already have revoked or minted. A timeout (60 s), a dropped connection, an
  unreadable answer to success, or a server fault now says "исход неизвестен · проверьте" (outcome
  unknown, check) and keeps the button locked until a rescan. A refusal the server explains is still
  "failed" and can be retried.

### Added

- [docs/design/ACCESS.md](docs/design/ACCESS.md): the access model for the dashboard server, the
  keyserver, MCP and the CLI. Each rule names the test that proves it, and each gap is stated as a
  gap. There are new tests showing that a declared caller name is a label and never an authority,
  that another workspace's token is refused, and that a cross-origin preflight is never granted.

## 0.3.9 — 2026-09-24

### Fixed

- The event collector read its retention horizon from `store/retention.json`, a file no workspace
  has. Retention reads `config/retention.json`. Without a horizon the collector inserted every commit
  within its depth, and retention deleted the old ones again, about 29,000 events on every tick of
  one installation. The collector now reads the same file. T25's guard is new: it builds a real git
  history with one commit past the horizon and one fresh, and proves that only the fresh one is
  collected. The old end-to-end check passed on any machine without an estate.
- `tools/trap_efficacy.py` ran in no public installation. It crashed when the private trap registry
  (`docs/knowledge-pack.md`) was absent, when a trap id was not `T<n>`, and when a self-driving suite
  was missing. It also wrote its report into the engine directory. It now says what it can't measure,
  sorts both id populations, and writes to `store/raw/trap-efficacy.json`. In this distribution it
  measures 3 caught, 1 missed (T25, whose guard is vacuous without a real estate) and 3 self-driven,
  and it reports 32 as inconclusive because their guards are not shipped.
- The T20 anchor now points at the public `tests/test_retention.py`, and T33, whose subject and
  guard are private, is no longer declared. `tests/test_trap_anchors.py` fails the day a source
  anchor goes stale.

## 0.3.8 — 2026-09-24

### Fixed

- The local OpenRouter and embedding key files followed `store/` even when `OBSERVATORY_STATE`
  redirected the workspace's state, unlike the token and salt. They now follow the selected state.
  A key left at the old location is still read with a note, and nothing is moved. A key in both
  places refuses instead of choosing silently. The installer refuses while a legacy copy exists,
  before it reads the key or asks the provider anything. Both locations are scanned for leaks.

## 0.3.7 — 2026-09-24

### Added

- The projects table groups each project's Heroku apps by the environment they serve, with production
  first and the account shown on hover. An app with no known environment is shown as "окружение не
  указано" (environment not specified), never guessed from its name. This is the last slice of
  [docs/design/DEPLOYMENTS.md](docs/design/DEPLOYMENTS.md).

## 0.3.6 — 2026-09-24

### Added

- A credential edge says where its value is read. Every `credential_used_by` edge carries a
  `binding`:
  - `local`: a vault slot, a destination file, or a file inside the project, all on this machine.
  - `unknown`: a curated edge that says the project uses the key but not where.
  - `run`: a production config var whose salted fingerprint equals a vault slot's current value, so
    that slot is read at run time by that deployment. The edge names the deployment and the variable.

  An edge also carries `environment` when a real slot is filed under one. `run` edges need a
  `remote-env` scan made with 0.3.6 or later, because the scan now also fingerprints the vault's
  current values. `build` is part of the contract but nothing scans CI secrets yet. This is the third
  slice of [docs/design/DEPLOYMENTS.md](docs/design/DEPLOYMENTS.md).

## 0.3.5 — 2026-09-24

### Added

- Environments are entities, scoped by project. `registry/environments.json` lists
  `environment:<project>/<name>` from explicit evidence only. There are four kinds:
  - an override in `config/environments.json`;
  - the Heroku pipeline stage, which the scan now reads;
  - the environment a project's vault slots are filed under;
  - a `.env.<name>` file in its checkout.

  Only the first two bind an app to an environment, as a `serves` edge that carries its rule. An app
  with neither is `unassigned`, and its name is not taken as a hint. So two projects' production apps
  no longer look alike. This is the second slice of [docs/design/DEPLOYMENTS.md](docs/design/DEPLOYMENTS.md).

## 0.3.4 — 2026-09-24

### Fixed

- The dashboard server reported version 0.1.0 in `/health`, in its receipt and in its `Server` header
  in every release. It now reports the application version, and a test refuses a second version
  literal anywhere in the engine.

## 0.3.3 — 2026-09-24

### Added

- Provider accounts are entities. `registry/accounts.json` lists each Heroku team (or a personal
  account) and each Cloudflare account under the provider's own id, and every app and zone the provider
  attributed gets an `in_account` edge that carries its rule. A resource whose account wasn't stated
  is listed as `unattributed` with the reason, never attached to the only account known. The Heroku
  scan now records the team id and, for a personal app, the owner's user id. Apps from an older scan
  stay unattributed until the next Heroku scan. This is the first slice of
  [docs/design/DEPLOYMENTS.md](docs/design/DEPLOYMENTS.md).

## 0.3.2 — 2026-09-24

### Fixed

- The leak scan read the memory companion's whole database on every tick. On a busy disk that step
  alone took more than half an hour. It now reads rows added since its last pass, as it already did
  for transcripts. A complete pass returns when the set of known values changes, weekly, when a table
  is recreated, or with `--full`. The scrub and the leak scan share this rule (`tools/sqlite_scan.py`).
- The engine's comments and docstrings are back. The source export had blanked about 8,300 lines that
  mentioned private context. They are restored where that was safe and rewritten in neutral words
  where it was not, so no line of spaces is left in their place. Code is unchanged: each file's AST,
  with docstrings removed, is identical.
- `check_public_release.py --private-denylist` is fast with a long list. It compiles the list once,
  where before it built one pattern per value for every file and blob.
- The 0.3.1 notes named the scrub's mark `state/scrub-watermark.json`. It is `store/scrub-watermark.json`.

### Added

- The tick's health, judged from outside the tick: `ok`, `running`, `running-long`, `interrupted`,
  `stale`, `never` or `disabled`. It appears in `full doctor`, in the server's `/health` and, when
  the data may be older than it looks, in every MCP answer's `degraded`. A tick killed by a restart
  used to leave the previous report in place, looking current.
- `tools/public-identifiers.json`: names this repository publishes on purpose, each with a reason.
  The privacy check subtracts them from a maintainer's private list and refuses an entry without a
  reason.
- `docs/design/DEPLOYMENTS.md`: the contract for accounts, environments, deployments and credential
  bindings (planned for 0.4).

## 0.3.1 — 2026-09-23

### Fixed

- Every derived relation names the rule that produced it (`rule` on `implemented_by`, `deployed_to`
  and `credential_used_by`), so an edge can be traced to its evidence; `validate` now refuses one
  without it.
- Two Cloudflare accounts holding a zone of the same name produced one zone id, and one of the zones
  vanished from the registry. Such zones are now `zone:<name>@<account>`; unique names keep their id.
- The ENV page showed the production verdict of only the first Heroku app sharing a checkout. It now
  lists every app's verdict for each variable.
- The memory-companion scrub re-read every row of both stores on every tick, testing each cell
  against each known value in turn; on a 5 GB store that step alone ran past half an hour. It now
  screens whole pages at once and, after one complete pass, reads only rows added since
  (`store/scrub-watermark.json`). A complete pass returns when the value set changes, weekly, when a
  table is recreated, or with `--full`. It also reads each store once instead of twice.

### Added

- `deployed_commit` on each Heroku app: the commit its last code release states it deployed, with the
  release number and source. Rollbacks, promotions and branch-only descriptions give `null`, never a
  guess from the configured branch. The Heroku page shows the short commit under the deploy date.

## 0.3.0 — 2026-09-23

### Added

- **Project ids survive renames.** `registry/identity.json` persists which names and strong anchors
  (a repository, a repository's former name, the root commit of a local Git history, a checkout path)
  belong to each project id. A renamed wiki folder, a transferred repository or a renamed local Git
  folder keeps its id, so history, notes and curation stay together. Ids are never reused; a project
  gone from a complete scan is retired, and a partial scan retires nothing. Anything ambiguous gets a
  new id and an `identity.ambiguous` finding instead of a guess. `identity_overrides.json` still
  wins. Upgrading changes no id. Contract: [docs/design/IDENTITY.md](docs/design/IDENTITY.md).

## 0.2.9 — 2026-09-23

### Fixed

- Projects whose names slug to one key (a wiki folder `Foo-Bar` and a repository `foo/bar`, or
  local folders `a b` and `a-b`) are all kept. 0.2.8 kept the last and dropped the others silently;
  now the highest-precedence anchor keeps the key, the others get a numeric suffix, and the collision
  is reported as degraded so you can pin names in `identity_overrides.json`.
- Commits in every checkout of a repository are recorded, not only the primary clone's: a worktree or
  second clone on another branch held work that never reached the event history.
- Relation ids use the project id. A project pinned with `identity_overrides.json` kept its id across
  a rename, but its `implemented_by` and `public_domain_of` edges were renamed with the merge key.

## 0.2.8 — 2026-09-23

### Changed

- MCP SDK 2.2.0 (`mcp`, `mcp-types`), with `requirements-full.lock` refreshed as one tested set:
  httpx2/httpcore2 2.13.1, starlette 1.7.0. The full offline matrix, including the MCP wire and
  contract suites, passes on the new set. The build backend may use setuptools up to 84.

## 0.2.7 — 2026-09-23

### Fixed

- The `ledger` step of every scheduled tick failed with `ValueError`: `export_ledger.py` printed the
  exported file relative to the program directory, and in a workspace installation the registry is
  elsewhere. The ledger was written; the step reported failure. It now names the file wherever it is.

## 0.2.6 — 2026-09-23

### Fixed

- launchd jobs keep Homebrew's `/opt/homebrew/bin`: 0.2.3's PATH filter dropped every
  group-writable directory, and Homebrew's is user-owned and admin-writable, so `heroku` and other
  Homebrew tools vanished from scheduled runs. Group-writable directories are kept when this user or
  root owns them; world-writable ones are still dropped.
- `full agent install` brings an already installed older plugin to the version the engine ships;
  `claude plugin install` succeeds without upgrading, so 0.2.5 left 0.11.0 in place.

## 0.2.5 — 2026-09-23

### Fixed

- The scheduled tick and the plugin hooks run with the Python that installed the engine
  (`OBSERVATORY_PYTHON`, written by `install_launchd.py` and `full agent install`). An installed
  package has no `.venv`, so tick fell back to the first `python3` on PATH, which lacks the `[full]`
  dependencies: the vector index reported "sqlite-vec is not loadable here". Companion plugin 0.11.1.

## 0.2.4 — 2026-09-23

### Changed

- The engine's comments and docstrings are back: 3,049 lines restored from the original sources
  that the first public export had blanked, filtered for private identifiers, with every file's
  syntax tree unchanged.

### Added

- `full doctor` reports `coverage_warnings`: an enabled integration or feature whose source is not
  configured or does not exist. A migrated installation without a `sessions` source had its leak
  scan narrowed from every agent transcript to four files without any warning. ONBOARDING now lists
  every source.

## 0.2.3 — 2026-09-23

### Fixed

- launchd jobs written by `install_launchd.py` and `serverd.py --install` carry the installing
  user's safe `PATH` directories, so collectors find `claude`, `heroku` and similar tools installed
  outside the system directories; before, the MCP inventory degraded with "claude CLI not on PATH".

## 0.2.2 — 2026-09-23

### Fixed

- **`full migrate-local` works again for installations that have a fingerprint salt or keyserver
  token.** 0.2.1 created fresh identities while staging the new workspace and then refused to copy
  the originals over them, so the migration stopped (`File exists`) and nothing was written. The
  original identities now move unchanged, and only a missing one is created, so fingerprints stay
  comparable across the move.
- Two processes opening a new database at the same moment no longer fail one of them with a
  `DatabaseError`: the version check that runs before the upgrade lock ignores a file another
  process is still creating, and the check under the lock decides.

## 0.2.1 — 2026-09-23

A correctness and security release. An audit of the published 0.2.0 wheel found fixes that had been
made in the private predecessor but never reached the public engine. All of them are carried here,
each with a regression test that fails on 0.2.0.

### Security

- **A local `rotate` no longer marks a leaked credential as closed.** In 0.2.0, `vault.py rotate`
  settled every open leak of the slot it replaced, although replacing a local slot proves neither
  that the provider revoked the old value nor that its consumers moved. `settle` (and
  `moved --settle`) now require `--revocation-evidence` and `--consumer-evidence`, recorded as manual
  attestations. **Upgrading reopens** every leak that 0.2.0 closed only by a local rotate: it shows
  as "closed only by a local rotate" at warning severity until you settle it with evidence.
- **The fingerprint salt and keyserver token are never created by a read.** A missing, empty,
  malformed, loosely permissioned or symlinked identity file is refused and left untouched.
  Identities are created only by `full init` (race-free), and re-running `full init` on an existing
  workspace keeps them and restores only a missing one.
- **Fingerprints taken under different salts are not compared.** Scans record a
  `fingerprint_namespace`; a comparison across namespaces answers `not_compared` with a
  `namespace_withheld` finding instead of a misleading "differs".
- **A private home inside the installed code or its checkout is refused.** 0.2.0 accepted an
  `OBSERVATORY_HOME` inside `site-packages`, where an upgrade or uninstall deletes it, or inside a
  source checkout, where it can be committed.

### Fixed

- A corrupt `finding_acks.json` is preserved: `ack` refuses to write, and the board builds without
  acknowledgements and names the unreadable file, instead of overwriting every saved decision.
- A former project id claimed by two projects is left unresolved rather than handing one project's
  history to the other by row order.
- A session that matches two projects equally is left unattributed (`ambiguous-project`) instead of
  being credited to whichever came first; a stale link is withdrawn by an event.
- The dashboard reads wallet and provider health from `OBSERVATORY_STATE`, where providers write
  them; ENV-page filters open the matching groups; counts use correct plural forms.
- The dashboard root `/` redirects to `/dashboard/index.html`, so its stylesheet and script load.
  Served at `/`, 0.2.0's pages opened unstyled and without data.

### Added

- `project-observatory full open [--serve]` builds the dashboard if needed and opens it, as local
  files or from the loopback server; it refuses to reuse a port that serves another workspace.
- `project-observatory full agent install|status|uninstall` installs the `observatory-log` Claude
  Code plugin from this repository with **auto-update on by default** (`--no-auto-update` opts out)
  and points its hooks at your workspace. The repository root is now a plugin marketplace.
- `tools/update_inventory.py` keeps the engine source inventory current; CI runs it with `--check`.
- CI installs the built wheel without the lock file and runs the dependency-sensitive suites.
- New regression suites: audit regressions, runtime identity, remote env, project identity,
  sessions, ENV page, metric labels, agent plugin.

### Upgrade

```sh
python -m pip install -U -c requirements-full.lock '.[full]'   # or the 0.2.1 release wheel
project-observatory full upgrade                                # preview, then --apply --writers-stopped
project-observatory full init                                   # creates any missing runtime identity
project-observatory full agent install                          # optional: plugin with auto-update
```

A 0.2.0 workspace is read by 0.2.1 as it is. A workspace created by 0.2.1 records 0.2.1 as its minimum reader.
The companion plugin is 0.11.0.

### Not established by this release

Live provider revocation and rotation, admission by external MCP hosts, and off-device restore were
not exercised; checks run on synthetic offline fixtures. Known-value scanning cannot prove that no
secret remains.

## 0.2.0 — 2026-09-21

First public release of the complete engine. See [docs/RELEASE.md](docs/RELEASE.md).

## 0.1.0

Portable CLI. See [docs/RELEASE-0.1.md](docs/RELEASE-0.1.md).
