# The person's surfaces: home, agents, access, sharing and the personal cloud (OBS-260 to OBS-274)

Status: design, 2026-10-26. Home (OBS-261) is built (`observatory/engine/dashboard/home_page.py`); the rest
is not yet. Each part is a row of
[the backlog](../backlog.md) (OBS-260 to OBS-274, and OBS-276 split from OBS-264) and a scenario of
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

Measured on `main` at `4e29af3e`. Paths are under `observatory/engine/`, except `macos/`, `desktop/`
and `site/`, which are at the repository root.

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

**Built so far (OBS-264, OBS-276, the command line):** `full share create|status|confirm|differ|revoke|
provider-ended|accept|list|sync` over the contract's `fabric-peer-share/1` (`node/peer_share.py`, run
over the contract's vectors), both stores (`node/shares.py`), a client and an in-process mailbox for
`observatory-cloud/1` (`node/mailbox.py`), and the use path `use_secret.py run --share`. The deployed
mailbox is OBS-263; the screen OBS-265; the landing page and the scheme OBS-266.

The protocol is D2, the link D3. **Data:** the share records of the local node (sent and received)
under the workspace's node folder, the cloud's delivery counts, the recipient's signed usage
reports, the provider's spend through the door (OpenRouter today), and `logs/secret-use.jsonl` on
the recipient's side.

## The secret prompt (OSS-84, OBS-260)

The agent asks Observatory, Observatory asks the person, the agent learns only that the slot is
filled. D4 decides how the window is shown on each system. The property is exact: the value stays
out of the chat, the agent's transcript, the logs and the agent's tool results. It does not stay out
of the commands the agent runs with it: an agent that runs `use_secret.py run ... -- printenv` prints
the value into its own output. Observatory removes known values from the output of `use_secret.py`
runs (OSS-48), which narrows that, and the use is journalled; it does not make it impossible.

```
+-- Project Observatory ----------------------------------------+
|  claude-code asks for a key                                    |
|  Project   billing-service      Slot  production / STRIPE_KEY  |
|  Why       "deploy the webhook handler"                        |
|  Your phrase: "blue heron at noon"                             |
|  Paste the token:  [**************************]                |
|  It is stored in the vault, not in the agent's chat.           |
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
- **The contract answered (DEC-0142, `fabric-peer-share/1`, pinned at `v0.1.0+2026-10-27.3`).** The
  engine follows it where it departs from this design's first draft, each departure with the
  contract's reason: no `share:` context kind (below); both sides commit, then both reveal; the code
  is the first five bytes of the transcript's SHA-256, and the claim's MAC and the code's transcript
  are over RFC 8785 objects with a `type`; the claim deadline is at most 7 days; the lease at most
  24 hours; an envelope at most 90 days; a usage report carries no random nonce; requests to the
  mailbox are signed with DEC-0056's construction and the share keys, not with the node's `#sig`.
- **The envelope.** `fabric-peer-share-envelope/1` (PS-7): the construction of the secret envelope,
  HPKE base mode, suite `[16, 1, 1]` (DHKEM(P-256, HKDF-SHA256), HKDF-SHA256, AES-128-GCM), to the
  recipient's share `#kem`, `info` the ASCII `fabric-peer-share-envelope/1`, the header as additional
  data, signed ES256 over the RFC 8785 bytes of `{header, ct}` by the sender's share `#sig`. The header
  names the share, the slot, the provider, `iat`, `exp` (at most 90 days) and a sequence per share and
  slot for anti-rollback, never the value. Its `info` differs from every other envelope's, so a share
  never opens as a personal placement or an organization's credential, and the reverse.
- **Held apart from every vault, and a key pair per share.** The contract adds no context kind
  (PS-3): `contextId` stays `personal` or `org:<id>`, and a received share is neither. The
  recipient's node keeps it under `node/shares/received/<id>/`, apart from every context's vault,
  and serves it only through its own use path (`use_secret.py run --share`), which counts each use
  for the report. Each side makes a fresh `#sig` and `#kem` pair for that one share and never uses its
  device keys, so two shares, or a share and an organization enrollment, cannot be linked by a key
  (PS-2, contract DEC-0128's rule of an identity per membership, applied to shares). The sender's share key is pinned by the recipient from the
  original link only when that link reaches the app untouched (D3); a handoff through the cloud's
  landing page is protected by the commit-reveal code alone.
- **The person's acts on this machine, stated plainly.** The command line keeps the comparison code
  out of `--json` and piped output, asks the sender to type the digits the recipient read aloud
  (never its own), and takes no answer to `confirm`, `accept` or `provider-ended` as an argument. A
  program running as the same user can still drive a pseudo-terminal, read the screen, or read the
  share's owner-only files (the code, the share key, the sealed copies). The CLI does not claim to
  stop it; the comparison moves into the signed app's window (OBS-280, after OBS-265 and OBS-266),
  where the secret prompt's window already draws the same line (D4).
- **Serving.** The recipient's node keeps the ciphertext sealed on disk and opens it in memory at
  each use, for the child process `use_secret.py` starts. It may reuse a ciphertext while its lease
  lasts (one hour by default, set by the sender, at most 24 hours, and never more than 24 hours from
  its grant whatever the mailbox says); renewing the lease asks the cloud whether the share is still
  active and counts a delivery. When the sender rotates a shared key, the sender's node seals
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
- **Usage reports (OBS-276, after the core).** The recipient's node sends, at each renewal and at
  least daily, a record per key signed by its share key and **sealed to the sender's share `#kem`**, so
  the cloud stores it without reading it: uses per day, last use, the consumer labels (agent names)
  from its secret-use journal, and exports made with the export button (OSS-82). Never a value, a
  command, an argument or an output (the rule of `call-record/1`, DEC-0111 item 6). Each report carries
  a sequence, and its header is the additional data its signature covers, so the cloud can neither
  move a report between shares nor play one again; the sender's node refuses a sequence that does not
  advance (PS-8; the contract dropped the random nonce as redundant). The sender's screen shows these as "reported by
  their machine", beside the cloud's own delivery count. A modified recipient can under-report; the
  delivery count and the provider's spend cannot be hidden by it, which is why they are shown beside.
- **Export, honestly.** "Exported" means "exported with the export button", a person's act on the
  recipient's screen, recorded and reported before the value leaves. It is not the only way a value
  leaves: any command an agent runs with the key can print it (`use_secret.py run ... -- printenv`),
  and that is a use, not an export. So the sender's screen never says "exported: no" as a guarantee; it
  says "exports with the button: none" and, beside it, the rule that every delivered value must be
  treated as copyable. A sender may mark a share "no export button"; that is a request the honest
  client keeps, not a control.
- **What the cloud still learns.** Even blind to values and reports, the sender's cloud sees: the
  share's existence, its id, the sender's labels for the recipient and the keys (stored as the sender
  typed them; the build may seal them too), the number of keys, end dates, the times and frequency of
  every claim, renewal and report, and the IP addresses of both machines. Whoever controls the cloud
  account (the person, or Cloudflare) can read those.
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
  S->>C: create share (descriptor signed by the share #sig, no value)
  Note over S: link https://CLOUD/s/ID#c=CODE&k=FPR goes to the recipient by the person
  R->>C: claim: share #sig and #kem public keys, MAC under CODE, signed
  C->>S: claim waiting (on the sender's next poll)
  S->>S: check the MAC; person compares the six-digit code with the recipient
  S->>C: envelopes sealed to the recipient's share #kem
  R->>C: renew lease (signed, nonce); C counts a delivery
  C->>R: envelope; R opens in memory at use
  R->>C: usage report (sealed to the sender's share #kem, nonce)
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
- **The link:** `https://CLOUD-ORIGIN/s/SHARE-ID#c=CODE&k=FPR`. `CODE` is 128 random bits, single
  use, valid until the share's claim deadline (at most 7 days, PS-4). `FPR` is the RFC 7638 SHA-256
  thumbprint of the sender's share `#sig`. **It is pinned only when the original link reaches the app untouched:**
  the person pastes it into the app's "Open a share" field, or opens it with the scheme carrying the
  `k=FPR` the person can see is the one in the link they were sent. The `project-observatory://share`
  URL the landing page builds is built by the cloud's own script, so a fingerprint arriving that way
  proves nothing about the cloud, and a landing-page handoff is protected by the commit-reveal code
  below and by nothing else. The fragment is not sent in HTTP requests, so it stays out of request lines, proxy
  logs and referrers. **It is not hidden from the cloud:** the landing page is the cloud's own script
  and can read the fragment, so a compromised or malicious cloud can learn `CODE`. The design does not
  rely on the cloud being unable to read it; it relies on the next two points.
- **The claim is bound to the code holder and checked by the sender.** The recipient's node sends its
  share `#sig` and `#kem` public keys, its commitment, and `mac`, HMAC-SHA-256 keyed with the code's 16
  bytes over the RFC 8785 form of `{type: "fabric-peer-claim/1", shareId, keys, fpr}` (PS-5). The
  sender's node, not the cloud, checks the MAC; the cloud only relays the claim. The MAC alone does not
  stop a cloud that knows `CODE`, so the sender **confirms each claim before anything is sealed** with
  a six-digit code compared out of band, contract DEC-0084's construction under the share's own types
  (PS-6):
  1. the recipient's claim carries its commitment, `sha256:` and the SHA-256 of the RFC 8785 form of
     `{type: "fabric-peer-commit/1", shareId, role, keys, nonce}` over a 32-byte nonce;
  2. the sender's node answers with a commit frame carrying its share keys and its own commitment;
  3. each side reveals its nonce only after it holds the other's commitment, and checks the other's
     reveal against it;
  4. both apps show the code: the first five bytes of the SHA-256 of the RFC 8785 form of
     `{type: "fabric-peer-code/1", shareId, sender: {keys, nonce}, recipient: {keys, nonce}}` as a
     big-endian integer, modulo 1,000,000, six digits with leading zeros; the two people compare them
     by a call or a message thread they already trust.
  Each side's commitment is fixed before it learns the other's nonce, so a party in the middle cannot
  try key pairs until the codes match: each attempt is one guess with a chance of 1 in 10^6. **One
  attempt per link** (`peerExchangeVerdict`, run by both nodes and the mailbox): a frame out of order, a
  second claim, a reveal that does not open its commitment, an abort or the claim deadline passing
  burns the exchange and seals nothing; a new attempt needs a new share the sender makes. The
  recipient's app reveals a nonce for a share id once and never again, so a cloud posing as the
  sender cannot rerun the recipient's side for fresh codes. Each run also needs the person's Accept
  on the recipient's side.
- **The trust this gives, stated plainly.** It is trust on first use with a one-in-a-million residual
  per link. A cloud or anyone else who reads the link before the intended recipient can claim first
  with its own keys; the MAC check passes (they know `CODE`), and the six-digit code matches the
  recipient's only with probability 1 in 10^6, so the sender refuses and nothing is sealed. A sender
  who skips the comparison accepts that risk, and the screen says so. Once sealed, the values are
  readable only by the recipient's share key; the cloud never holds them in the clear.
- **The scheme:** it carries the share link's parts as the link does (PS-4): the share id in its
  path and `c` and `k` in its fragment only, never in a query, never logged, the code used once
  (the exact form lands with OBS-266). It is registered by the
  macOS app (`CFBundleURLTypes`) and by the desktop app through Tauri's deep-link support (the
  registry on Windows, an `x-scheme-handler` desktop entry on Linux). The app accepts only `share`,
  validates every parameter, refuses an unknown or repeated one, and opens a view; it never claims
  without the person's "Accept". This follows the grammar rules of contract DEC-0040 (FL-1 to FL-6)
  for its own scheme. Those rules refuse a credential in a `fabric://` link's fragment; the claim
  code is not a credential to any service, and this record keeps it out of every `fabric://` link.
  **Another app can register the same scheme** (no operating system reserves a custom scheme for one
  app), so the deep link is a convenience, not a trust anchor: what protects the share is the
  commit-reveal code, and the pinned `FPR` when the original link reached the app; all are checked
  inside Observatory.
- **"Install, then come back":** the page offers the scheme first; if nothing handles it within
  1.5 seconds (the test DEC-0040 FB-5 uses for Fabric), it shows the installer link and keeps the
  button: after installing, the person returns to the same tab and presses it again. A browser that
  blocks custom schemes gets the link as text for the app's "Open a share" field.

### D4. The native secret prompt is shown only by Observatory's own signed app

Built so far (OBS-260): the server half (#378): the agent binding (`full agent-binding issue NAME`,
`secret_prompt.py`), the node's queue with its limits and outcomes, `secret.request`, `secret.get` and
`secret.cancel` on `POST /mcp`, and `observatory_secret_request` with its job. Then, on macOS: the node
opens the enrolled app through the lifecycle broker (`BrokerPrompter`, the broker's own `secret-prompt`
caller), the app's door (`full assistant secret`, run as the app's own child and answering only when a
file lies inside the enrolled bundle and whose immediate parent, running, satisfies the enrolled app's
code requirement; a grandparent is never enough, since the app runs other engines for other actions), the `SecureField` window and the
person's phrase in the data protection keychain (the app's access group, signed in only with a
provisioning profile; a build without it answers `no-client`). How the value moves, exactly: the
window hands it to the engine process the app spawned, on that process's stdin; that process writes it
into the slot with the vault's own writer (`vault.store_value`, as `put` or `rotate`) and drops it; the
node reads only the slot's metadata. That pipe and child are inside the app's process tree, which is
what the door's parent check proves; a program of the same user that can change what the app runs
would be in that tree too, and owning a file is not running Observatory's engine (the review of #382
showed a system-owned `git` driven by a user `.gitconfig`). So the secret door never runs the
`executable` preference, PATH or a launcher: only the engine inside the app's own bundle
(`SecretEngine`, `Contents/Resources/engine/`), by an absolute path the app computes, after
`SecStaticCodeCheckValidity` on the app itself (all architectures, strict, nested code, sealed
resources, against the team named in its sealed Info.plist, not a compiled-in team) immediately before
each call, list and store alike, with a scrubbed environment. **No release bundles that engine yet:**
the node checks for it in the enrolled app, and that the bundle still satisfies its enrolled code
requirement, and otherwise answers every request `no-client`, reason `engine-untrusted`, with the
person's `vault.py put` step, before filing and before opening the app.
**What the check does not cover.** The signature check hashes the bundle once, and a Python engine
then reads its `.py` files as it imports them (`client_store` imports the vault writer after the value
arrived). A drag-installed app belongs to its user, so a program of that user with App Management can
swap a file after the check and restore it afterwards: the check alone does not stop it. Before any
engine is bundled it must be one that cannot change after the check (copied into storage only the app
can write, or a hardened-runtime binary with library validation), or the bundle must be one the user
cannot write; its workspace (`OBSERVATORY_HOME`, today from the app's settings), its interpreter and
its dependencies must be inside the sealed bundle too; and it must run as the app's direct child, with no
launcher process in between (today's `project-observatory full` starts the engine as its own child, so the
door, which accepts only the app as its immediate parent, would refuse it). Whether the app is built with
the hardened runtime and library validation, so that no program of the user can inject into it before the
parent check, is checked at the same time.
The window is excluded from sharing (`NSWindow.sharingType = .none`) and the phrase from the
accessibility tree; a program with Screen Recording may still capture it on recent macOS. This
release's app is not signed with the provisioning profile the keychain group needs, so it keeps no
phrase and answers `no-client`, reason `phrase-store-unavailable`; the follow-up is that the release
fetches or creates the Developer ID profile through the App Store Connect API with the key it already
holds. Not built: the Windows desktop window (the node has no lifecycle
broker on Windows yet, OBS-277, and the window is opened only through it), so Windows answers
`no-client`, reason `broker-unsupported`; Linux stays `no-client` (DEC-0143 SP-2). The queue's states and
outcomes are those of the contract's draft `secret.prompt` channel method (DEC-0143, SP-1 to SP-7), so
the channel can replace the broker path below at the next contract pin without changing the agent's
tool.

- **The path of a request, until the contract decides a channel method.** The agent calls
  `observatory_secret_request` (a `fabric.job.*` job) on the MCP server, which is a child of the
  agent's process. That server shows nothing itself and never touches a value: it records the request
  (agent from its binding, project, slot, provider, reason) in the node's request queue. The node is the
  only Observatory process that writes the queue, but the queue is an owner-only file, so any program
  running as the same user can read and write it: read every request's text, file a request under any
  agent's name with any reason, or set an outcome, bypassing the binding and the `engine-untrusted`
  gate. It cannot see a value typed into the genuine window. **This is new work (OBS-260):** today the stdio MCP
  server has no binding of its own (it is the built-in `local:stdio` principal shared by every local
  agent, `observatory/engine/access_binding.py`), and the node's `POST /mcp` serves only the
  `node.lifecycle` tools behind a lifecycle grant (`tools/serverd.py`). OBS-260 adds a binding per local
  agent, issued when the agent is connected, and a `secret.request` tool on `POST /mcp` that takes the
  agent's name from that binding. The limit, stated plainly: a binding's token file is owner-only, so
  any agent running as the same user can read another agent's token and file a request under its
  name, the same limit as the phrase on Linux and Windows; the prompt still shows the slot and the
  reason, and nothing is stored without the person typing it. The node asks
  the lifecycle broker (OSS-76) to open Observatory's own code-signed app with the request id, never a
  direct launch. The app reads the request from the node, shows the window, and the value goes into the
  vault slot without crossing the local channel or reaching the node: the window hands it on stdin to
  the engine process the app itself spawned, which writes it as `vault.py put` writes it (the status
  note above says exactly how, and what that does not protect against). The job then reads the outcome, `stored`,
  `declined` or `no answer`; no value is ever in an argument, an environment variable, a log, a
  notification or the result. When the node is down, the request fails with "Observatory is not
  running" and no window is shown by anyone.
- **Why not DEC-0075's `consent.prompt`.** The local channel's consent prompt carries
  `{promptId, purposeId, noticeVersion, deadline}`, is answered `given` or `denied` only, goes to every
  `consent.ui` client, and carries no request text and no value (the channel's specification,
  consent section). A secret request needs a method of its own with client admission at `hello`
  (only the signed app may declare it), not a check at answer time. That method is part of the
  OBS-262 ask; until the contract decides it, the broker path above is used.
- **No scripted dialogs.** `osascript` (`display dialog ... with hidden answer`), `zenity` and
  `kdialog` are rejected: any process of the same user, an agent included, can draw an identical
  window and collect what the person types. A prompt that any agent can imitate teaches the person to
  paste tokens into imitations.
- **macOS.** The app's own SwiftUI window with a `SecureField`. **Linux and Windows.** The desktop
  app's (Tauri) window. Both are the signed apps the release already builds.
- **The person's phrase.** At first use the person sets a short phrase. It is kept in the signed
  app's own store, never in a vault slot. **On macOS** it is an item of the data protection keychain
  limited to the app's access group, which another process of the same user cannot read (a legacy
  login-keychain item is not used: it can be released to an agent through a `security` prompt the
  person approves). **On Linux and Windows no store separates one app of a user from another:** a
  Secret Service item in an unlocked collection, and DPAPI, which is per user, are readable by any
  agent running as the same user. There the phrase only keeps an agent that does not look for it
  from imitating the prompt; the design says so on the setting itself, and the stronger defence on
  those systems is to run agents as another user or in a container. A vault
  slot will not do, because `use_secret.py` releases a slot to any caller unless a grant is required
  (`slotsRequireGrant` defaults to `when-token`, `tools/use_secret.py`), so any agent could read the
  phrase and draw a convincing imitation. Every genuine prompt shows the phrase; a window without it
  is not Observatory. It is changed in the app's Settings, never shown in a tool result.
- **A machine with no desktop session** (a server, a remote shell): an agent's request is **refused**
  with "no desktop to ask the person on" and the step for the person: run `vault.py put PROJECT ENV
  NAME` in their own shell and paste the value on its stdin. There is no terminal answer to an agent's
  request, because an agent can drive a terminal through a pseudo-terminal, would see the phrase, and
  could type a value of its choosing; a person putting a value in by hand is not answering an agent.
- **Against a fake request:** the prompt names the agent from its binding (not from text the agent
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
- **Public code search (off until the person turns it on).** Eligibility is decided per provider
  format, from a table the build adds (prefix, body length, alphabet, checksum part): a key is
  eligible when its random body, after the fragment is removed, still carries at least 112 unknown
  bits. For each eligible key, a fragment of **8 characters** at a random offset inside the random
  body (never the provider's public prefix, never a checksum tail) is chosen **once** and its offset
  kept in the slot's metadata. The arithmetic at the floor of a 32-character body: from 36 symbols the
  body holds about 165 bits, 8 characters disclose about 41, about 124 stay unknown; from 62 symbols,
  about 190, 48 and 143; a 64-character hex body holds 256 bits and 8 characters disclose 32. One fixed
  fragment means repeated searches never disclose more, and 36^8 (about 2.8 x 10^12) makes an
  unrelated public string containing it unlikely, so a hit is a strong candidate. Sources must match substrings: public code search services that do (grep.app, public
  Sourcegraph); GitHub's REST code search matches whole tokens, so a fragment does not find a key
  there, and GitHub is covered by the provider signals instead. A hit's file is fetched, candidates
  are extracted by the provider's format, and each is compared in memory with the vault value; only
  the URL, the line, the commit and "matches slot X" are kept.
- **Recall is low, and the screen says so.** Public code indexes cover a part of public code, with a
  delay, and none covers pastes, chats or private places. A search that finds nothing reads "not found
  in the sources searched on DATE", never "not leaked".
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
  lines compressed with the standard library's `lzma`, encrypted with the backups' `OBSENC1` format
  under **an archive key of its own**: 32 random bytes kept in the OS credential store, never in a vault
  slot. On macOS it is a data protection keychain item limited to the signed app's access group, so
  only the app can read it: archiving and restoring run in the signed app, and the engine's Python
  archiver asks the app to do it (with the app not running, nothing is archived and nothing raw is
  deleted). On Linux and Windows the same limit as the phrase applies: any agent running as the same
  user can read the item, and the design says so where the archive is set up. It is never in a vault
  slot because `use_secret.py` releases a slot to any agent unless a grant is required
  (`slotsRequireGrant` defaults to `when-token`), and never in the workspace. It is not the backup passphrase: that one is a plain file in the
  workspace's `secrets/` that any agent of the same user can read, and when it is absent the backups
  write nothing outside the workspace. Written beside the backups with a manifest of counts and
  SHA-256. Values of known vault slots are replaced by their slot names before writing, as the
  store's redaction does. Kept two years by default.
- **No archive key:** nothing is archived and nothing is deleted; raw rows stay in the store past the
  window, and Health says "agent records: not archived, no archive key" with the command that makes
  one.
- **Restoring:** `full agents archive restore MONTH --into DIR` builds a separate read-only database
  for analysis; nothing is ever restored into the live store.
- **How raw is actually removed.** A tombstone does not free disk: `store/retention.py` keeps the
  canonical `ledger` row and removes only its read path and indexes. So archiving uses its own step,
  and only for the classes it archives (session records, collected events, journals; never a ledger
  row of a workflow, a lesson, an operator row or a `superseded` row): after a month's archive is
  written, decrypted again and its counts and digests match the rows, the rows are deleted in one
  transaction, a receipt row records the month, the counts and the archive's digest, and a `VACUUM`
  returns the pages. Journal files (`secret-use.jsonl` and the like) are cut at the archived offset and
  rewritten atomically. A failed check deletes nothing. Horizons live in `retention.json`; an agent
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
  landing page. Every request is signed with contract DEC-0056's construction (PS-10) under the label
  `fabric-peer-share`, `keyid` the thumbprint of the signing side's share `#sig` (never the node's
  device key); a share's creation is also signed under the label `observatory-owner` by the owner key
  the node makes once under `node/shares/owner/` and registers at deploy, and recipients' keys are
  learnt at their claim. The routes are the engine's (`node/mailbox.py`), since the contract leaves
  them to the product (CT-185). The code ships in the engine's wheel, pinned by version and digest.
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
| DEC-0123 | every dashboard page is handed over as Fabric Dashboards' `open_link` |
| DEC-0075 | its `consent.prompt` carries no request text or value and goes to every `consent.ui` client, so it is not used for secrets; a secret-request method with admission at `hello` is part of the OBS-262 ask, and until then the node opens its signed app through the lifecycle broker and the value never crosses the channel |
| DEC-0128 | a key pair per share, never the device keys, so shares cannot be linked |
| DEC-0008 | sharing, minting, revoking and deploying are effects with scope; none runs without the person's act or a recorded grant |
| DEC-0142 | `fabric-peer-share/1`, the answer to OBS-262: a key pair per share, the link and its pin, the claim MAC, the commit-reveal code with one attempt per link, the envelope, usage reports, revocation and deletion, and what the mailbox may see and do (PS-1 to PS-10); no context kind; the mailbox's routes are the product's (CT-185); who pays for a share's use is open (CT-187) |
| DEC-0143 | the local channel's `secret.prompt` (secret requests), the other half of the OBS-262 ask; adopted by OBS-260's app half |

## The build order

The person's order was sharing and the cloud first, then the agents screen, Home, the secret prompt,
the infrastructure pages, leak watch, and the log lifecycle; the person also asked for practical
results soon. So the contract ask goes out first (sharing's format depends on its answer), and
four local items that need nothing new land in the first week of November as visible wins: the
secret prompt (agents stop asking for tokens in chats, and sharing's recipients use it too), Home's
first screen, and the Cloudflare and DigitalOcean pages. Sharing's core follows the cloud, its usage
reports come after the core, and the rest is ordered by date.

| Order | Row | What | Target |
|---|---|---|---|
| 1 | OBS-262 | ask the contract for the share profile and the cloud service | 2026-10-28 |
| 2 | OBS-260 | the secret prompt (local, quick) | 2026-11-04 |
| 3 | OBS-261 | Home: state, three problems, projects at a glance (local, quick) | 2026-11-04 |
| 4 | OBS-271 | Infrastructure: Cloudflare page, one shape for the three (local, quick) | 2026-11-04 |
| 5 | OBS-235 | Infrastructure: DigitalOcean collector and page (existing row, moved up) | 2026-11-04 |
| 6 | OBS-263 | the personal cloud service and its deploy | 2026-11-07 |
| 7 | OBS-264 | sharing core: create, claim, seal, use, stop, revoke | 2026-11-14 |
| 8 | OBS-276 | sharing: usage reports sealed to the sender | 2026-11-21 |
| 9 | OBS-265 | the Shares screen: create, mint, usage, take back | 2026-11-21 |
| 10 | OBS-266 | the recipient's path: link page, URL scheme, notice, export | 2026-11-21 |
| 11 | OBS-267 | Agents: available agents and Switchboard | 2026-11-28 |
| 12 | OBS-268 | Agents: activity per agent | 2026-11-28 |
| 13 | OBS-269 | Access: Use | 2026-11-28 |
| 14 | OBS-270 | labels and their ownership | 2026-12-05 |
| 15 | OBS-272 | leak watch | 2026-12-12 |
| 16 | OBS-273 | the monitor | 2026-12-19 |
| 17 | OBS-274 | the agents' log lifecycle | 2026-12-23 |

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
