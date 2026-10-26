# The person's surfaces: home, agents, access, sharing and the personal cloud (OBS-260 to OBS-274)

Status: design, 2026-10-26. Nothing here is built yet; each part is a row of
[the backlog](../backlog.md) (OBS-260 to OBS-274) and a scenario of
[portable-scenarios.md](../../observatory/engine/docs/ux/portable-scenarios.md#the-persons-surfaces-sharing-cloud-agents-access-home-2026-10-26)
(OSS-79 to OSS-93).

The person who runs Observatory asked, on 2026-10-26, for the product a person uses rather than the
one an agent reads. Observatory is the infrastructure and access product of the family: what runs,
what it costs, which keys reach what, and which agents act on it. Today its pages answer an agent's
questions well and a person's poorly. The home page is a wall of findings (eight critical rows above
the counters), the Agents page shows internal workflows a person did not start and cannot read, and
the things a person most needs to do (give an agent a token, give a colleague access, take it back,
know that a key leaked) need a terminal and the right command.

This record decides the screens, the data each one reads, and the hard parts: the sharing
protocol, the share link and deep link, the native secret prompt, the leak search, the agents' log
lifecycle, label ownership and the connection to a personal cloud. The screens follow the
internal-screen rules IS-01 to IS-18 of the super-ux pack (one question per screen, scope and
freshness on every number, empty, loading, failed and full designed together).

## What exists today

Measured on `main` at `4e29af3e`. Paths are under `observatory/engine/`.

| Need | What exists | Where |
|---|---|---|
| Page list and navigation | twelve pages in four groups (Work, Infrastructure, Access, System) | `dashboard/shell.py` `PAGES`, nav groups |
| Home | findings preview of eight, moved above the counter tiles at build time; activity tiles; no projects at a glance | `dashboard/shell.py` (`OVERVIEW_FINDINGS_LIMIT`, `page_html`) |
| Agents page | counters, "Needs you", workflows with executor lanes and handoffs, sessions of the last day | `dashboard/agents_page.py`, `agents_view.py` |
| Installed agents and Fabric services | `scan-agents` reads every service descriptor, its manifest and launchd into `registry/agents.json` (OSS-72); no page renders it | `collectors/scan_agents.py`, `fabric_service.py` `services_dir()` |
| Installed coding agents | the harness scan's coding-agent catalogue into `store/raw/harness.json` | `collectors/scan_harness.py` `coding_agents()`, `tools/harness.py` |
| Switchboard | known only to the lifecycle broker by its bundle id; nothing scans for it | `node/lifecycle/broker.py` |
| Session records | one ledger record per session, revised each turn | `tools/record_turn.py`, table `ledger` (`kind='session'`) |
| Retention | tombstones, never DELETE; operator rows exempt; horizons in `defaults/retention.json`; logs rotated 5 x 5 MB | `store/retention.py`, `log_policy.py`, `tools/rotate_logs.py` |
| Vault | slots `PROJECT/ENV/NAME` with `meta.json`; `leaks.jsonl`, `movements.jsonl` | `tools/vault.py` |
| Secret-use journal | `logs/secret-use.jsonl`, one row per use, refusal, failure, serve or header; names only | `tools/use_secret.py` (`AUDIT`), read by `tools/agent_secret_findings.py` |
| Keys page | credentials with leak, owner, sharing, rotation and disabled filters, and the door verbs as commands | `dashboard/build_dashboard.py` `renderCreds` |
| Leak scan | local files, transcripts and the store matched against the full plaintext values, in memory | `tools/scan_leaks.py` |
| Leak register | open leaks and their settlement (OSS-55) | `tools/vault.py`, `leak_register.py` |
| Secure input | none: no native prompt; `getpass` only for backup passphrases; the keyserver (OSS-09) refuses vault `put` | `tools/keyserver.py` |
| Doors | Cloudflare mints narrow tokens (16 presets, `workers-edit` among them) but deploys nothing; DigitalOcean cannot mint by API and installs from stdin; Heroku issues OAuth presets; OpenRouter mints keys with a ceiling | `tools/cloudflare.py`, `tools/digitalocean.py`, `tools/heroku.py`, `tools/openrouter.py` |
| DigitalOcean and Cloudflare pages | none; DigitalOcean appears as finding titles, Cloudflare as zones on Domains | `dashboard/finding_types.py`, `renderDomains` |
| Node keys and envelope | `#sig` (ES256) and `#kem` (ECDH P-256) per device; `fabric-secret-envelope/1` sealed with HPKE to a target's `#kem`, signed by the sender's `#sig` | `node/device_keys.py`, `node/envelope.py`; [NODE.md](NODE.md#the-secret-envelope-obs-113-slice-4) |
| Estate digests | `HMAC-SHA-256(estate key, value)` per slot, compared across machines (OBS-194 step 1); step 2 (the sealed transport between one person's machines) not built | `tools/vault_digests.py`; [VAULT-DIGESTS.md](VAULT-DIGESTS.md) |
| Labels | none; `organization` per project (declared, then rule, then external) and products as groups; agents propose `organization`, `resources`, `aliases`, the person accepts | `organizations.py`, `proposals.py`, `review_policy.py` |
| Website and URL scheme | the public site redirects its home to the product page on passioncode.ai; no app registers a URL scheme; the macOS app is one SwiftUI window around the dashboard, the desktop app (Tauri) has a tray | `site/_redirects`, `macos/Sources/ObservatoryApp`, `desktop/src-tauri` |
| Monitoring | DNS, HTTP and registry checks of domains; the disk guard every 15 minutes; Notification Centre for new critical findings | `collectors/scan_domains.py`, `tools/disk_guard.py`, `tools/notify_findings.py` |

## The rules these screens keep

1. **A value never travels where a person or an agent can read it by accident.** Not in a chat, an
   argument, a URL, a log, a notification, a report, a usage record or the cloud's storage. An agent
   receives a value only at use time, through `use_secret.py`, in the environment of the command it
   runs ([AGENT-SECRETS.md](AGENT-SECRETS.md)).
2. **Measured and not measured are different states.** A tile, a row or a check that did not run
   says so; it is never green, zero or "none".
3. **Who measured it is part of the number.** "Seen by your cloud" and "reported by their machine"
   are separate columns; a provider's figure names the provider.
4. **What the person set is theirs.** An agent may add; it never replaces what the person set
   (D7).
5. **Every button is also an agent's tool** (contract DEC-0121): each action below has an MCP tool or
   a door verb, and an approval the person gave in chat is recorded with their words.

## Navigation

```
Home
Work            Projects . Agents . Findings
Infrastructure  Heroku . DigitalOcean . Cloudflare . Domains . Traffic
Access          Keys . Shares . Use . Leak watch . ENV . MCP
System          Health . Machine . Personal cloud
```

"Overview" becomes "Home". Shares, Use, Leak watch, DigitalOcean, Cloudflare and Personal cloud are
new pages; Agents is rebuilt. A page with nothing connected still exists and says what connecting it
needs (UX5 of [UI-PLAN.md](../ux/UI-PLAN.md)).

## Home (OSS-88, OBS-261)

**The question:** is everything fine, and what needs me first? A monitoring product is opened to
check (IS-03's stated exception), so Home is a summary on purpose, but every tile is a link to the
page where the work happens.

```
Home                                       measured 2026-10-26 17:26 UTC
+-----------+-----------+-----------+-----------+-----------+-----------+
| Sites     | Apps      | Disk      | Keys      | Agents    | Cloud     |
| 92 up     | 58 up     | 8 GB free | 3 leaked  | 2 working | connected |
| 3 down    | 3 down    | memory ok | 12 old    | 1 stalled |           |
+-----------+-----------+-----------+-----------+-----------+-----------+
Needs you first
  1. [critical] app web-worker is paid for and not serving   [Open app]
  2. [critical] Disk: 8 GB free (1.6%)                            [Free space]
  3. [critical] A leaked key was never rotated (42 days)          [Rotate]
  All problems (N)  ->  Findings
Projects at a glance            group by: organization | label | none
  Client A (12)   3 need you   last activity today
  Personal (40)   0 need you   last activity yesterday
Activity  (commits, sessions, deploys, last 7 days)
```

| State | What the page shows |
|---|---|
| Empty (fresh install) | no tiles; "Connect your first source": projects folder, a provider door, the agent plugin, each with its command; the page never shows a row of zeros |
| Loading | the tile grid and three finding rows as skeletons of their final size (IS-07); the page is static HTML, so this is only the live server's refresh |
| Failed | a source that failed keeps its tile with "not measured since TIME" and the reason; other tiles render |
| Full | as drawn; "Nothing needs you" with the time of the last check when no finding is open |

**Data:** sites from `registry/domain-liveness.json`; apps from `registry/heroku-apps.json` and the
DigitalOcean collector of OBS-235; disk and memory from `machine_view.summary()`; keys from
`registry/credentials.json` and `leaks.jsonl`; agents from `agents_view.summary()`; cloud from the
connection record of OBS-263; findings from `registry/findings.json` and `config/finding_acks.json`;
projects from `registry/projects.json`, `organizations.py` and the labels of OBS-270; activity from
the store's `events`.

**Which three.** The order is severity, then whether the person can act now (an action exists and no
human gate blocks it), then how long it has been open, then how many projects it touches. Findings of
one kind collapse into one row with a count ("3 leaked keys not rotated"), so three rows are three
different problems.

## Agents (OSS-85 to OSS-87, OBS-267, OBS-268, OBS-274)

**The question:** which agents do I have, and what are they doing to my things? Three tabs:
Available, Activity, Logs. Workflows (OSS-24 to OSS-27) stay as a fourth tab for the person who
follows handoffs.

```
Agents      Available | Activity | Workflows | Logs
Switchboard: installed, not running     [Start]   what it adds: accounts, limits, handing work over
Coding agents on this machine
  Claude Code   2.1   2 sessions today   last turn 12 min ago
  Codex         0.9   no session this week
Fabric services on this machine
  copywriter    answers   0.4.2   local      events: 12 today
  analytics     no answer since 09:12        [Restart through the lifecycle broker]
```

| State | What the page shows |
|---|---|
| Empty | "No agents found here" only when the harness scan and the services folder were both read; otherwise "not scanned yet" with the command |
| Loading | the static page plus the live server's refresh, as the Agents page polls today |
| Failed | a descriptor or a harness source that could not be read is a row of its own, named |
| Full | as drawn |

**Available.** Coding agents from `store/raw/harness.json` (`coding_agents()`); Fabric services from
`registry/agents.json` (`scan_agents.py`); each service's health from its `/.well-known/fabric-service`.
Switchboard is detected by three independent signs, any of which counts: its Fabric service
descriptor in the services folder, its app bundle (the identifier the lifecycle broker already knows),
and its MCP server in the MCP inventory (OSS-46). States: connected (its service answers), installed
but not running (start goes through the lifecycle broker, never a direct launch), not installed (the
PassionCode setup's one command, and what Switchboard adds).

**Activity, per agent (OSS-86).** Branches created, files changed per repository, permission requests
and their answers, errors by kind, keys used (from the secret-use journal), sessions. Sources:

| Fact | Source today | Gap filled by OBS-268 |
|---|---|---|
| sessions, turns, project, account | `ledger` `kind='session'` (`record_turn.py`) | none |
| branches created | not recorded | the session's repository and `git for-each-ref` at session start and end, recorded by the Stop hook |
| files changed | not recorded | the paths of the harness's edit and write tool calls, read from the turn by `record_turn.py`, relative to the repository |
| permission requests and answers | not recorded | the harness's permission and notification hook events, recorded by the `observatory-log` plugin |
| errors | not recorded | tool errors and API errors of the turn, by class, without their text |
| keys used | `logs/secret-use.jsonl` | joined by the consumer label the agent runs under |
| a Fabric service's own events | not collected | `/fabric/v1/events` of each local service, read with the descriptor's `auth.tokenFile` (never printed), into the store |

**Logs (OSS-87):** D6.

## Access: Use (OSS-91, OBS-269)

**The question:** did my agents use keys the way the rules say? One row per agent and key: uses
through the vault, refusals, failures, `.env` fallbacks (forbidden under `--vault-only`), the last
use, and the findings of `agent_secret_findings.py` and the leak scan (`secret.seen_outside_its_home`).
Empty: "No agent has used a key through Observatory yet", with the skill to install. Failed: the
journal could not be read, named. The page states its blind spot: an agent that used a key outside
Observatory appears only where the leak scan saw the value.

## Sharing (OSS-79 to OSS-82, OBS-262, OBS-264 to OBS-266)

**The questions:** for the sender, who has my keys and what are they doing with them; for the
recipient, what was shared with me and how do my agents use it.

```
Shares      Sent | Received                           [New share]
Sent
  Alex (laptop)   active     2 keys   41 uses / 7 d   last use 2 h ago   exported: no   [Take back]
  Contractor      waiting for the recipient, link created today           [Copy link] [Cancel]
Alex (laptop) > translation-api (OpenRouter, $20 ceiling)
  uses per day     . . 4 9 12 6 10        reported by their machine
  deliveries       38                       seen by your cloud
  spend            $3.10 of $20             read from OpenRouter
  agents           claude-code, copywriter
```

| State | Sender | Recipient |
|---|---|---|
| Empty | "You have not shared a key" and what sharing does in two sentences | "Nothing was shared with you" |
| Waiting | for the recipient; for your machine to seal | for the sender's machine |
| Active | usage as drawn | keys, their end date, the command an agent uses |
| Failed | cloud unreachable, sealing failed, provider rotation failed, each with its retry | link used elsewhere, expired, revoked, each with the date |
| Ended | expired or taken back, with what was confirmed (D2) | removed from this machine, with the date |

The protocol is D2, the link D3. **Data:** the share records of the local node (sent and received)
under the workspace's node folder, the cloud's delivery counts, the recipient's signed usage
reports, the provider's spend through the door (OpenRouter today), and `logs/secret-use.jsonl` on
the recipient's side.

## The secret prompt (OSS-84, OBS-260)

The agent asks Observatory, Observatory asks the person, the agent learns only that the slot is
filled. D4 decides how the window is shown on each system.

```
+-- Project Observatory ----------------------------------------+
|  claude-code asks for a key                                    |
|  Project   billing-service      Slot  production / STRIPE_KEY  |
|  Why       "deploy the webhook handler"                        |
|  Paste the token:  [**************************]                |
|  It is stored in the vault. The agent never sees it.           |
|                                      [Cancel]   [Save]         |
+----------------------------------------------------------------+
```

## Personal cloud (OSS-83, OBS-263)

States: not connected (what it is for, what it holds, the button), deploying (steps with progress,
surviving navigation, IS-15), connected (address, version, last contact, shares it carries, cost
none on the free plan or the provider's figure), unreachable (since when, what stops working:
new shares and lease renewals), moving (to the PassionCode personal cloud, later). D8 decides the
connection.

## Infrastructure (OSS-90, OBS-271)

DigitalOcean is OBS-235 (its collector and its page). Cloudflare gets a page from a read-only token
(a new read preset of the door) collected into `registry/cloudflare-account.json`: Workers, Pages
projects, R2 buckets, D1 databases and zones, each with its account, state and the findings that
concern it; zones keep their place on Domains, which links here. Every infrastructure page has the
same columns (resource, project, account, state, cost, findings) so that the three read as one product
(IS-18). Empty: the door's read preset command; failed: the provider's refusal and its time.

## Leak watch (OSS-92, OBS-272)

Per key: eligible or not (and why), last check, sources checked, result. D5 is the method.

## Labels (OSS-89, OBS-270)

A "Labels" control on projects, keys, sites and agents; group-by on Home and Projects. D7 is the
ownership rule.

## Monitoring (OSS-93, OBS-273)

D10. Its output is Home's "hot" line, the tiles' states and one notification per change.

## Decisions

### D1. Home shows the state, three problems and the way to the rest

- **Options.** (a) Keep the findings first, shorter. (b) A grid of counters only. (c) State tiles,
  the three first problems, projects at a glance, activity.
- **Chosen: (c).** The person asked for exactly this, and IS-01 agrees: one question, answered in
  the first screenful. Eight critical rows answered "what is wrong" and hid "is anything fine"; a grid
  of counters (b) hides what to do. Three is a limit, not a sample: the ranking above makes the three
  the ones to do first, and "All problems (N)" is always one press away.

### D2. Sharing: sealed to the recipient's machine, served on demand, taken back honestly

- **Options for where the value lives.** (a) Copy the value into the recipient's vault once.
  (b) The cloud holds the value encrypted with a key it also holds. (c) The value is sealed to the
  recipient machine's `#kem` key; the cloud holds only that ciphertext and its metadata, and serves
  it on demand under a lease.
- **Chosen: (c).** (b) makes the cloud a holder of secrets, which the person's rule forbids and which
  makes every cloud breach a key breach. (a) gives the sender nothing to stop: once copied, the value
  is beyond reach. (c) keeps the cloud blind, gives the sender a real stop for every use that goes
  through Observatory, and a count it observes itself.
- **The envelope.** The construction of `fabric-credential-envelope/1` (contract DEC-0111 item 3):
  HPKE base mode, suite `[16, 1, 1]` (DHKEM(P-256, HKDF-SHA256), HKDF-SHA256, AES-128-GCM), to the
  recipient's `#kem`, the header as additional data, signed ES256 over the RFC 8785 bytes of
  `{header, ct}` by the sender's `#sig`. The header names the share, the slot label, the provider,
  the end date and a sequence for anti-rollback, never the value. Until the contract defines a
  person-to-person profile (OBS-262), the engine uses its own `info` string,
  `observatory-share-envelope/1`, so that a share can never be opened as a personal placement or an
  organization's credential, and the reverse.
- **The recipient's context.** A shared value belongs to a context of its own, `share:SHARE-ID`,
  never the recipient's personal context (the same separation contract DEC-0111 keeps for an
  organization's credentials). The sender's `#sig` is not in the recipient's estate record, so it is
  pinned at the claim from the share's signed descriptor, and only that key may seal into that context.
- **Serving.** The recipient's node keeps the ciphertext sealed on disk and opens it in memory at
  each use, for the child process `use_secret.py` starts. It may reuse a ciphertext while its lease
  lasts (one hour by default, set by the sender); renewing the lease asks the cloud whether the share
  is still active and counts a delivery. When the sender rotates a shared key, the sender's node seals
  the new value to every active recipient, and their next renewal picks it up.
- **Taking back.** In this order, each step shown with what was confirmed:
  1. the cloud refuses every renewal and delivery for the share at once (observed by the sender's own
     cloud);
  2. the recipient's node deletes the ciphertext and the lease at its next contact and returns a
     signed deletion receipt (reported by their machine);
  3. **the provider rotation:** a value that reached another machine can have been copied by that
     machine's owner (from the process environment of a running command, or by a modified client), so
     stopping service ends access through Observatory and does not unlearn a copy. Only the provider
     can end a copied value's power: a key minted for the share is revoked at the provider through its
     door (`openrouter.py revoke`, `cloudflare.py revoke`, `heroku.py revoke`; DigitalOcean's
     `revoke` confirms by a 401); a key of the sender's own is rotated (`vault.py rotate` names its
     consumers). The screen says "taken back" only when steps 1 and 3 are confirmed.
- **Why minting is recommended.** A key minted for one share can be revoked without touching anyone
  else, carries its own ceiling, and its spend is the share's spend. Sharing an existing key works but
  the screen names its other consumers before the share is created.
- **Usage reports.** The recipient's node sends, at each renewal and at least daily, a signed record
  per key: uses per day, last use, the consumer labels (agent names) from its secret-use journal,
  and whether the value was exported (OSS-82). Never a value, a command, an argument or an output
  (the rule of `call-record/1`, DEC-0111 item 6). The sender's screen shows these as "reported by
  their machine", beside the cloud's own delivery count. A modified recipient can under-report; the
  delivery count and the provider's spend cannot be hidden by it, which is why they are shown beside.
- **Export.** A person's act on the recipient's screen, never an agent's tool. The export is recorded
  and reported before the value leaves; a sender may mark a share "no export", which the honest client
  enforces and the screen describes as a request, not a guarantee.
- **One machine.** A share binds one recipient machine. A recipient's second machine needs its own
  share, until their estate can carry it between their machines (OBS-194 step 2).
- **What a subscription is not.** A subscription sign-in (a coding agent's monthly plan) is a person's
  account under its provider's terms and is never shared. "Give someone Claude Code access" is an
  OpenRouter key with a ceiling that Claude Code can use, minted for the share.

```mermaid
sequenceDiagram
  participant S as Sender's node
  participant C as Sender's cloud
  participant R as Recipient's node
  participant P as Provider
  S->>P: mint narrow key (door), value into the vault
  S->>C: create share (descriptor signed by #sig, no value)
  Note over S: link https://CLOUD/s/ID#c=CODE goes to the recipient by the person
  R->>C: claim (proof of CODE, recipient #sig and #kem public keys, signed)
  C->>S: claim waiting (on the sender's next poll)
  S->>C: envelopes sealed to the recipient's #kem
  R->>C: renew lease (signed); C counts a delivery
  C->>R: envelope; R opens in memory at use
  R->>C: usage report (counts, signed)
  S->>C: take back; C refuses from now on
  S->>P: revoke or rotate (door)
  R->>C: next contact: deletion receipt
```

### D3. The share link, the website and the deep link

- **Measured:** no app registers a URL scheme today; the public site is static and redirects its home
  to the product page on passioncode.ai.
- **Options for the landing page.** (a) A page on passioncode.ai that every share link points to.
  (b) A page served by the sender's own cloud service, at the share's own address. (c) No page: send
  the deep link itself.
- **Chosen: (b).** The share already lives at the sender's cloud, so its link needs no third host,
  and a person who does not have Observatory still gets a page that works in any browser. (c) fails
  for exactly the person who has not installed the app. (a) adds a dependency on a site the sender
  does not control; it stays as the install target, not the landing.
- **The link:** `https://CLOUD-ORIGIN/s/SHARE-ID#c=CODE`. The code is 128 random bits, single use,
  valid until the share's claim deadline (7 days by default). It is in the fragment so it never
  reaches a server, a proxy log or a referrer. It grants one thing: being the first machine to present
  its keys for this share. It holds no provider credential and opens nothing by itself.
- **The scheme:** `project-observatory://share?origin=ORIGIN&id=SHARE-ID#c=CODE`, registered by the
  macOS app (`CFBundleURLTypes`) and by the desktop app through Tauri's deep-link support (the
  registry on Windows, an `x-scheme-handler` desktop entry on Linux). The app accepts only `share`,
  validates every parameter, refuses an unknown or repeated one, and opens a view; it never claims
  without the person's "Accept". This follows the grammar rules of contract DEC-0040 (FL-1 to FL-6)
  for its own scheme. Those rules refuse a credential in a `fabric://` link's fragment; the claim
  code is not a credential to any service, and this record keeps it out of every `fabric://` link.
- **"Install, then come back":** the page offers the scheme first; if nothing handles it within
  1.5 seconds (the test DEC-0040 FB-5 uses for Fabric), it shows the installer link and keeps the
  button: after installing, the person returns to the same tab and presses it again. A browser that
  blocks custom schemes gets the link as text for the app's "Open a share" field.

### D4. The native secret prompt is shown by Observatory, never by the agent

- **The rule:** the window runs in a process the agent did not start and cannot read: the node (the
  local server) or the Observatory app. The agent's call (`observatory_secret_request`, a job under
  `fabric.job.*`) returns `stored`, `declined` or `no answer`; the value goes from the window to the
  vault slot as `vault.py put` writes it, on a pipe, never in an argument, an environment variable, a
  log, a notification or the job's result.
- **macOS.** Options: (a) `osascript` with `display dialog ... with hidden answer`, the value read
  from the child's stdout; (b) the Observatory app's own SwiftUI window with a `SecureField`, asked by
  the node over the app's local channel. **Chosen: (b), with (a) as the fallback** when the app is not
  running. (b) carries the app's name and icon, which a spoofed dialog cannot, and needs no scripting
  permission; (a) works from any process today and reads the value over a pipe the node owns, never
  from argv.
- **Linux.** The desktop app's window when it runs; otherwise `zenity --password` or
  `kdialog --password` started by the node, the value on the child's stdout.
- **Windows.** The desktop app's window; otherwise `CredUIPromptForWindowsCredentialsW` called by the
  node, which returns the secret into the node's memory.
- **No desktop session** (a server, a remote shell): a one-time page on the local server (the
  keyserver's loopback origin rules, OSS-09), its address handed over as Fabric Dashboards'
  `open_link` (contract DEC-0123), valid for one submission and ten minutes. Never a chat.
- **Against a fake request:** the window names the agent from its binding (not from text the agent
  wrote), the project, the slot and the reason, quoted as the agent's words; a request for a slot that
  already holds a value says so and asks to replace; requests are limited (one open per slot, five per
  agent per hour).

### D5. Leak watch: provider signals first, then a fixed short fragment, compared at home

- **Options.** (a) Send the full key to a search service. (b) Send a hash of the key to a service that
  stores hashes of leaked keys. (c) Read the providers' own signals and the person's own repositories'
  alerts, and search public code by a short fragment, comparing candidates on this machine.
- **Chosen: (c).** (a) hands the key to a third party. (b) needs a service that hashes every public key
  it sees; none exists for API keys in general (unlike passwords), and a plain hash of a high-entropy
  key would be an exact identifier anyway.
- **Provider signals (on by default).** GitHub's secret scanning partner programme sends keys pushed
  to public repositories to their providers, many of which disable them. The doors' probes already
  see that (`ping`: a 401 or a disabled key), so a key that turned invalid without a rotation on
  record is a "provider says leaked or revoked" finding. Secret scanning alerts of the person's own
  repositories are read through the API with the person's GitHub sign-in; an alert's secret is
  compared in memory with the estate digest (VAULT-DIGESTS.md) and never written.
- **Public code search (off until the person turns it on).** Eligible keys: a known provider format
  whose random body is at least 32 characters from an alphabet of at least 36 symbols. For each one,
  a fragment of **8 characters** at a random offset inside the random body (never the provider's
  public prefix, never a checksum tail) is chosen **once** and its offset kept in the slot's
  metadata. 8 base-62 characters disclose under 48 bits of a body of at least 190 bits, which leaves
  more than 140 unknown bits; one fixed fragment means repeated searches never disclose more; and
  62^8 (about 2 x 10^14) makes an unrelated public string containing it unlikely, so a hit is a strong
  candidate. Sources must match substrings: public code search services that do (grep.app, public
  Sourcegraph); GitHub's REST code search matches whole tokens, so a fragment does not find a key
  there, and GitHub is covered by the provider signals instead. A hit's file is fetched, candidates
  are extracted by the provider's format, and each is compared in memory with the vault value; only
  the URL, the line, the commit and "matches slot X" are kept.
- **Revocation.** A confirmed sighting is recorded in the leak register (`vault.py leak`, OSS-55) and
  raises the critical finding with the door's rotate command (`rotate --leaked` for OpenRouter).
- **Never:** a fragment in a log, a finding, a report or the dashboard; a search query with a project
  name; a key shorter than the rule allows sent anywhere.

### D6. Agents' records: raw for a month, daily aggregates for long, monthly sealed archives

- **What is raw:** the session records in the `ledger`, the store's `events`, `logs/secret-use.jsonl`,
  the keyserver's audit, the Fabric services' collected events, the activity facts of OBS-268. The
  harness's own transcripts are the harness's: Observatory reads them in place and does not copy or
  delete them (its retention is the harness's setting).
- **What is aggregated:** one row per day, agent and project in a new table: sessions, turns,
  branches created, files changed (count and the ten most changed paths), permission requests by
  tool and answer, errors by class, key uses by slot, spend where known. This is what the Agents page
  and Home read; it is small and kept 400 days.
- **What is archived:** raw rows older than the hot window (30 days by default), per month, as JSON
  lines compressed with the standard library's `lzma`, encrypted with the workspace's backup
  encryption, written beside the backups with a manifest of counts and SHA-256. Values of known vault
  slots are replaced by their slot names before writing, as the store's redaction does. Kept two years
  by default.
- **Restoring:** `full agents archive restore MONTH --into DIR` builds a separate read-only database
  for analysis; nothing is ever restored into the live store.
- **Order of operations:** a raw row is withdrawn (a tombstone, as `store/retention.py` does) only
  after its archive is written and its checksum verified. Horizons live in `retention.json`; an agent
  cannot change them. `full forget` reaches the live store and names archives it could not rewrite in
  its receipt, as it does for backups today ([FORGET.md](FORGET.md)).

### D7. Labels: the person owns what they set

- **Options.** (a) Last writer wins. (b) Agents only propose, through the review queue. (c) A label
  has an owner; an agent may set one where the person has none, shown as a suggestion; an agent's
  change to a person's label is a proposal.
- **Chosen: (c).** (a) breaks the person's rule. (b) makes every label a review item; agents label
  what they create (a project, a key minted for a share) far more often than a person does, and a
  queue of hundreds of trivial labels would bury the proposals that matter (a busy machine's review queue
  already holds hundreds of items).
- **The record:** `config/labels.json`, one entry per target and key: `{target, key, value, by:
  "person" | "agent:NAME", at, adoptedFrom?}`. A person's edit of an agent's label makes it the
  person's. Agents write through an MCP tool that refuses a target whose label for that key is the
  person's and files a proposal instead (`proposals.py`). The operator-owned guard of the registry
  applies: no agent path writes a `by: person` entry. Organizations stay as they are
  (`organizations.py`); a label is the person's own grouping beside them.

### D8. The personal cloud: self-hosted now, PassionCode's later, one protocol

- **Measured in the family's plans:** the personal cloud is a VM PassionCode hosts and the person
  claims with a passkey, granted on request today with self-serve payment later; the relay is built
  but not deployed; no contract decision covers a cloud service for one person's Observatory, or
  sharing between people.
- **Options.** (a) Wait for the PassionCode personal cloud. (b) Use the relay as the transport.
  (c) A small Observatory cloud service the person deploys into their own Cloudflare account with the
  existing door, later served by the personal cloud. (d) A Git repository or gist as the store.
- **Chosen: (c).** (a) blocks the first thing asked for on a product not yet self-serve. (b) carries
  messages between one estate's machines while both are connected; it does not store, and a recipient
  outside the estate cannot use it. (d) puts share metadata where it is listed and forces a recipient
  to hold access to it. (c) needs only an account the person already connects, costs nothing on
  Cloudflare's free plan for this load (to be confirmed at build: Workers and Durable Objects with
  SQLite storage), and keeps the data in the person's own account.
- **The service.** One Worker with one Durable Object per share and one for the owner, speaking
  `observatory-cloud/1`: register owner keys, create, claim, seal, renew, report, revoke, and the share
  landing page. Every request is signed by a node's `#sig` over method, path, body digest and time;
  the owner's public key is written into the Worker's configuration at deploy, recipients' at their
  claim. The code ships in the engine's wheel, pinned by version and digest.
- **Deploying.** The Cloudflare door gains the deploy step: a narrow token (`workers-edit` plus the
  Durable Object binding it needs) minted from the stashed admin into a slot, the script uploaded
  through Cloudflare's API, the token kept for updates and revocable on disconnect. An engine update
  that ships a new service version redeploys it after the person's next confirmation.
- **Later.** When the person has the PassionCode personal cloud, the node running there serves the same
  `observatory-cloud/1`. Moving copies the ciphertexts and metadata as they are (they are sealed to
  machines, not to the host) and leaves a signed forwarding record at the old address for 90 days. The
  sealed transport between one person's own machines (OBS-194 step 2) uses the same envelope and the
  same service when the relay is not deployed.

### D9. The Agents screen reads what is already collected, and adds four facts

Available agents come from two scans that exist (harness and services) and one sign for Switchboard
that is added; activity adds branches, files, permissions and errors through the Stop hook and the
plugin's hooks, because the transcripts are the harness's and reading them for every page would be
slow and would copy their text. The Workflows tab keeps OSS-24 to OSS-27 unchanged.

### D10. The monitor is the maintenance job, not a model

- **Options.** (a) A language-model agent that reads everything on a schedule. (b) A deterministic
  watcher in the maintenance job (every 15 minutes) that compares states and raises one notification
  per change, with the assistant (and its budget) asked only for the daily summary the person turns on.
- **Chosen: (b).** Watching must not spend money or depend on a provider being up, and a change of
  state is computed, not judged. The watcher reads the sources Home reads, adds key validity through
  the doors' `ping` (at most once a day per key) and the personal cloud's last contact, and writes
  through `notify_findings.py` (Notification Centre today, Fabric's inbox when the service is
  connected).

## Contract matters

| Contract | How it applies here |
|---|---|
| DEC-0047, DEC-0053 | device keys by role (`#sig`, `#kem`) and the secret envelope's construction, reused for shares |
| DEC-0111 | the credential envelope's construction and checks, the separate context for credentials that are not the person's own, revocation that names its request, the call record's no-content rule for usage reports |
| DEC-0040 | the link grammar the share scheme follows; no credential in a `fabric://` link |
| DEC-0121, DEC-0126 | every button of these screens is an agent's tool; an approval relayed from the person's own agent is the person's |
| DEC-0123 | the no-desktop secret page and every dashboard page are handed over as Fabric Dashboards' `open_link` |
| DEC-0008 | sharing, minting, revoking and deploying are effects with scope; none runs without the person's act or a recorded grant |
| none yet | a person-to-person share profile (a context kind, the sender's key pinned at claim, revocation across estates) and a single-person cloud service: OBS-262 asks the contract for both |

## The build order

The person's order was sharing and the cloud first, then the agents screen, Home, the secret prompt,
the infrastructure pages, leak watch, and the log lifecycle. Two local and quick items move first
because they unblock the person now: the secret prompt (agents stop asking for tokens in chats this
week, and sharing's recipients use it too) and Home's top three (a working first screen in place of
a wall of every finding). The contract ask goes out at once because sharing's format depends on its answer.

| Order | Row | What | Target |
|---|---|---|---|
| 1 | OBS-260 | the secret prompt, macOS first (local, quick) | 2026-11-07 |
| 2 | OBS-261 | Home: state, three problems, projects at a glance (local, quick) | 2026-11-14 |
| 3 | OBS-262 | ask the contract for the share profile and the cloud service | 2026-10-30 |
| 4 | OBS-263 | the personal cloud service and its deploy | 2026-11-14 |
| 5 | OBS-264 | the share protocol on the node and the cloud | 2026-11-21 |
| 6 | OBS-265 | the Shares screen: create, mint, usage, take back | 2026-11-28 |
| 7 | OBS-266 | the recipient's path: link page, URL scheme, notice, export | 2026-11-28 |
| 8 | OBS-267 | Agents: available agents and Switchboard | 2026-12-05 |
| 9 | OBS-268 | Agents: activity per agent | 2026-12-12 |
| 10 | OBS-269 | Access: Use | 2026-12-12 |
| 11 | OBS-270 | labels and their ownership | 2026-12-12 |
| 12 | OBS-271 | Infrastructure: Cloudflare page, one shape for the three | 2026-12-19 |
| 13 | OBS-272 | leak watch | 2027-01-09 |
| 14 | OBS-273 | the monitor | 2027-01-16 |
| 15 | OBS-274 | the agents' log lifecycle | 2027-01-23 |

## Interface text

The sentences quoted on these screens (the recipient's notice, the secret prompt, the used-link and
install-then-return messages, "Nothing needs you") are drafts. They passed Copylot's deterministic
check in English (report `59d2651f-886a-4b80-8192-342736fd63bf`, register `ui`, no findings); each
lands through `dashboard/locales/` with its Russian and is checked again in both languages in the
change that ships it.

## Not decided here

- The prices or limits of a hosted cloud, and who pays for it: the family's decision.
- Sharing with an organization rather than a person: an organization's credentials travel by
  contract DEC-0111 and the organization store (contract CT-140), not by shares.
- Whether the "no export" mark becomes a contract term, which only a client both sides trust can keep.
- Which public code search services stay in leak watch: measured at OBS-272, by recall on planted
  test keys and by each service's terms.

## What is never done

- A value in a chat, an argument, a URL, a log, a notification, a report, a usage record, the cloud's
  storage or the dashboard.
- A shared value written to disk in the clear on the recipient's machine.
- "Taken back" or "deleted" shown when it was not confirmed.
- A subscription sign-in shared.
- An agent's label over the person's.
- A full key, or more than its one fixed fragment, sent to any third party.
