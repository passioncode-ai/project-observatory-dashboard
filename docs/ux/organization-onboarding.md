# Organization onboarding: from personal to an organization (OSS-102 to OSS-109)

Status: draft, 2026-11-08. Scenarios: OSS-102 to OSS-109 in
[the full-engine scenario base](../../observatory/engine/docs/ux/portable-scenarios.md#personal-to-organization-onboarding-projects-and-keys-2026-11-08).
Build rows: OBS-411 to OBS-420 and the existing rows named in the [gap map](#gap-map) below, on
[the backlog](../backlog.md). Product outcome: unobserved; nothing here is built yet unless a row says so.

The operator asked on 2026-11-08 how Observatory is set up for a person who will connect to an
organization, and how a personal install converts into an organizational one: the person chooses
which projects and which keys go to the organization, keeps the rest personal, and nothing about it
should be unclear. He also said that GitHub organizations, DigitalOcean teams and Observatory's own
"organizations" read as one blurred grouping. This document is the answer: one vocabulary, the rules
that make the conversion safe, the screens, and what each step needs that does not exist yet.

**Sources this design follows** (read before changing it):

- the organization's first release plan, fabric-workspace `docs/reports/2026-10-25-organization-r1/README.md`
  (onboarding order O-1 to O-4; O-3 is this flow; rows N-14 selection, N-15 encrypted store, N-17
  environments; forks F-11 one project in several organizations and F-12 who decrypts the store, both open);
- the estate review of 2026-11-06 (fabric-workspace `docs/plans/2026-11-06-estate-flows/README.md`): a key a
  person holds for an organization's agent is never copied into the organization's vault; the organization
  issues its own key and the old one is revoked, so a hand-over is a rotation; shares and personal snapshots into
  an organization's space are refused;
- the contract: contexts are `personal` or `org:<id>`, one context per project (`docs/specification/contexts.md`);
  device enrollment DEC-0031; the notice and its acknowledgement from the person's signed-in session, never a
  request body (DEC-0052 item 3); what may leave a person's Fabric at all, the OM-7 allowlist (DEC-0127 to
  DEC-0129: enrollment, check-ins, sessions in the organization's context, `grant.usage`, the node summary,
  credential requests and the chosen adaptation references); leaving revokes grants and deletes the context's
  credentials (OM-5); an organization's credential sealed to the enrollment (DEC-0111); a device is one counted
  OS account or one VM (DEC-0154);
- the operator's decision relayed on 2026-11-08: every macOS user and every virtual machine is a separate device;
- the hub's invitation and device pages: an invitation is single use, lives 72 hours and registers the person's
  first passkey; the Computers page («Компьютеры») prepares one computer's enrollment token, enrolls a fleet,
  lists computers with their state and revokes one; the hub stores provider accounts by name, owner and
  environments, never keys.

## Four words

The confusion the operator named comes from one word doing four jobs. Every screen, command and
document of this flow uses these four, and the Organizations page carries this table as its help.

| Word | What it is | Who decides it | Examples |
|---|---|---|---|
| **Account** | A container at a provider where resources and keys live. Measured, never invented. | the provider | a GitHub owner (a user or a GitHub organization), a DigitalOcean team (each `doctl` context points at one), a Cloudflare account, a Heroku team (or a user's own apps), an OpenRouter account (one provisioning key) |
| **Organization** | Who a project, its accounts and its keys belong to in Observatory. Always one named **Personal**, the default. Any other is **local** (a name you gave, to keep a client's or an employer's accounts apart; no server) or **joined** (the same name linked to an organization this space is enrolled with). | the person, from rules they confirmed | Personal; Acme (joined); Client B (local) |
| **Context** | The runtime boundary keys, enrollments and agent instances live in: `personal` or `org:<id>` (the contract's word). A joined organization is a context; a local one lives inside `personal`. | the contract | `personal`, `org:acme` |
| **Space** | One Observatory workspace, normally one OS account or one VM, which the organization counts as one device. A space may be bound to one context. | the person (or the machine's administrator) | the person's own macOS user; a second macOS user kept for Acme's work |

What follows from the table, and what the help says in one line each:

- **A GitHub organization is an account, not an Observatory organization.** Three GitHub organizations,
  two DigitalOcean teams and one Cloudflare account can all belong to Acme. One GitHub user account can hold
  both personal and Acme repositories; then its projects are decided one by one.
- **A DigitalOcean team is an account too.** A `doctl` context is how this machine reaches one team; a context
  with no stashed token is an account seen but not connected.
- **The grouping is Organization, then Account, then Project**, everywhere: Home, Projects, Keys.
  Today the Projects page groups by a project's first GitHub owner; OBS-418 replaces that.
- **Today's file `config/organizations.json` holds organizations in this sense** (Personal is the entry marked
  `default`), with match rules on repository owners and products only. OBS-412 adds the other account kinds,
  and OBS-413 links an entry to its enrollment, so a local organization becomes a joined one without a second name.

## Principles

1. **Personal by default.** Anything without a confirmed decision is personal and stays where it is.
2. **Rules propose, people decide.** A rule maps an account to an organization and proposes; it never moves a
   project or a key by itself.
3. **The organization learns only what was chosen for it.** Personal projects, keys, accounts and decisions are
   never sent, not as names, not as counts (OM-7).
4. **A key changes context only by rotation.** A key is never copied from one context to another; the
   organization issues its own, and the old value is revoked or stays personal (estate review, 2026-11-06).
5. **Every step can be undone, and says how.** Before anything is sent, undo is exact. After, undo is a return
   plus a rotation, because a value once seen cannot be unseen.
6. **Every button is also an agent's tool** (contract DEC-0121, the operator's rule of 2026-11-07). An agent does
   each step on the person's word and records it; only the steps that need the person's body stay with them.

## Business rules

Numbered for tests and reviews; a scenario names the rules it exercises. "Item" means a project, a key (a vault
slot) or an account.

| ID | Rule | How it is checked |
|---|---|---|
| ORG-R01 | An item with no confirmed decision is personal. A missing, unreadable or conflicting rule leaves the item personal and says why. | a fixture with no `organizations.json`, one malformed, one with two organizations matching an account: every item reads `personal` with its reason |
| ORG-R02 | Nothing is sent to an organization, and nothing enters its context on this machine, unless the person chose that item, or chose a group whose member list was shown at the time of the choice. A member added to the group later is proposed, never included. | the selection record lists each item it covers; an item absent from it is refused at apply |
| ORG-R03 | The part of a selection the organization receives names only the chosen items: for a project an opaque reference and, where its repository is in one of the organization's accounts, that repository; for a key a credential request (provider, scopes, environment, slot name), never a value. Personal decisions, names and counts stay on the machine. | the sent document is checked against an allowlist of fields before signing; a test plants a personal project name and fails if it appears |
| ORG-R04 | Each account belongs to at most one organization. An account two organizations claim, or a GitHub user account holding repositories of both, is **shared**: its projects and keys are decided one by one. | rule evaluation reports `conflict` or `shared` and assigns nothing |
| ORG-R05 | A project belongs to one context (the contract today; fork F-11 is open as CT-141). A project two joined organizations both claim waits for the person and is shown as a conflict. | the selection refuses a project selected for two organizations |
| ORG-R06 | A key's proposal combines its project's organization and its issuing account's organization (from the door's record). Both agree: proposed for that organization. They disagree, the account is shared, or the issuer is unknown while the project is the organization's: **ambiguous**, personal until chosen. | `full org keys` over fixtures of each case |
| ORG-R07 | A provider admin or provisioning key a door keeps (`secrets/<provider>-admin`, OpenRouter's provisioning key, a DigitalOcean team token) is never handed over. The organization's space stashes its own; a personal one stays personal. | the key list marks door admin keys `not movable` with the reason |
| ORG-R08 | Handing a key over is a rotation: a credential request goes to the organization, the replacement arrives into the organization's context sealed to this enrollment (DEC-0111) with its provenance (OBS-360), its consumers switch, then the personal value is revoked at the provider with proof, unless the person keeps using it personally. | a hand-over that would place the same value in two contexts is refused |
| ORG-R09 | A value found in two contexts or two spaces (equal keyed digests, OBS-194) is a finding until rotated into two values; it is never resolved by deleting one copy silently. | a planted duplicate raises the finding and offers the rotation |
| ORG-R10 | A name already present in the target context is never overwritten. Equal digests: nothing to do, recorded as already there. Different values: the organization's stays, the personal one stays personal, and the person may hand it over under another name. | apply over a fixture with a conflicting slot leaves both untouched |
| ORG-R11 | A selection is one record, `org-selection/1`: who decided (the person, or an agent with the person's words, time and place), when, each item with its decision and why (the rule, or by hand), the destination, and the digest of the record it replaces. Kept owner-only; the sent part is signed by the enrollment's `#sig` key. | the record verifies; a changed byte fails |
| ORG-R12 | Applying a record is idempotent and resumable: each item is `planned`, `done`, `failed` or `undone` in a journal; the same record again changes nothing; an interrupted apply continues where it stopped. | apply twice, kill mid-apply and resume, compare journals |
| ORG-R13 | Undo before anything was sent restores the previous record exactly. After sending, a return is a new record, and every key the organization held is rotated or revoked. | undo before send leaves no trace on the server; after send it produces rotation steps |
| ORG-R14 | The preview lists every item that moves, where it goes, what the organization will see, and one line proving the rest: "N projects and M keys stay personal". Apply is refused for a record the person has not previewed in its current form. | apply of a record whose digest differs from the previewed one is refused |
| ORG-R15 | Default selection: projects matched by a confirmed account rule are selected; ambiguous, shared-account and default projects are not. Keys are selected only when project and issuer agree (ORG-R06). | the window's initial state over the fixture set |
| ORG-R16 | A project or key found after the selection that a confirmed rule maps to a joined organization is proposed as a finding ("1 new project may belong to Acme"), never selected by itself. | a new checkout under an Acme-owned repository raises the finding and changes no record |
| ORG-R17 | Leaving, being removed or the organization closing ends the enrollment (existing, OSS-74) and then: the organization's credentials on this machine are deleted (OM-5); projects return to personal with the organization still shown as local; keys whose origin was personal are returned to personal and listed for rotation; no checkout, repository or file of the project is touched. | the leave plan over the fixture set; checkouts unchanged byte for byte |
| ORG-R18 | Joining needs the organization's notice acknowledged by the person in the hub's signed-in session (DEC-0052); Observatory shows the notice version and whether it is acknowledged, and never acknowledges it itself. Until then nothing about the person is collected. | a join with no acknowledgement reads `waiting for the notice` and collects nothing |
| ORG-R19 | Joining checks the organization's root key fingerprint the invitation carries against the server's, and shows it to the person to compare over a second channel. A mismatch stops before any key is made. | a planted wrong fingerprint stops the join with nothing written |
| ORG-R20 | A space bound to a context (OBS-359) refuses items of any other context; a mixed space keeps a context on every project and slot and the organization sees only its own (mode C, DEC-0118). | context check over a bound and a mixed fixture |
| ORG-R21 | Each OS account and each VM enrolls on its own token as its own device; one token never enrolls two spaces. | a second enroll with a used token is refused by the server and named |
| ORG-R22 | A key keeps its environment. Until environments are data (OBS-234) the environments are `local`, `stage` and `prod`; a slot in another environment is shown as such and needs a choice. | classification of a slot in `qa` reads `environment not declared` |
| ORG-R23 | Every action of the screens has a CLI command and an MCP tool with the same effect and the same refusals; an agent acting on the person's word records the words verbatim, the time and where they were said. The steps that need the person's body are named once each: registering the passkey from the invitation, acknowledging the notice in the hub, confirming the project selection with the passkey (the hub's rule that a project enters by the person's own passkey), the macOS administrator password when a new OS account is created, and comparing the fingerprint. | the parity test: every screen action maps to a door; the gate list is the same in the screen, the CLI help and the MCP tool descriptions |
| ORG-R24 | Offline: accounts, rules, the selection and its preview work without the network; local moves between spaces work offline; anything for the server waits in the enrollment's outbox and the screen says "waiting to send", never "done". | the apply journal with the server unreachable |
| ORG-R25 | A partial result is never shown as success: the result screen counts done, failed and waiting per kind, and each failed item names its step and command. | a fixture where one key's provider refuses the revocation |

## Where the organization's work lives

The person picks one of three layouts when they join; the screen recommends the first and shows the costs of
the others. The organization's own main Observatory lives in its own space (operator decision of 2026-11-06);
joining adds an organization beside `personal` and never replaces it.

| Layout | What happens | When it fits | Costs |
|---|---|---|---|
| **A separate space** (recommended) | A new OS account (or VM) on this computer, or another computer, becomes a space bound to `org:<id>` (OBS-362, OBS-359). The chosen projects' curated registry entries are exported into it (OBS-364), keys are handed over by rotation (ORG-R08), and the personal space forgets what moved once the import is confirmed (OBS-372). | the person works for the organization on a machine that is also theirs | a second login; the macOS administrator password once; two Observatory installs |
| **This space is the organization's** | The OS account Observatory runs in is already used only for the organization. It is bound to `org:<id>`; anything personal found in it is moved out to the person's personal space or forgotten, never sent. | a work account or a VM made for the organization | personal items must leave first; the selection shows them as "moves out" |
| **Both in one space** | One workspace keeps both contexts; every project and slot carries its context; the organization sees only its own (mode C). | a single-account machine where a second login is not possible | the person must keep the contexts apart; the organization's notice may say that two organizations on one computer can be linked (DEC-0154) |

## Screens

Each screen lists its states. The owner column says which product draws it: the Observatory desktop app and its
dashboard (Observatory), the hub's web pages (Hub), the PassionCode launcher (Launcher), or Fabric.

### ORG-W1. Organizations and accounts (Observatory; OSS-102, OSS-103)

The question: whose is everything here, and what is not decided yet?

```
Organizations                                    measured 2026-11-08 10:12 UTC   [?] What these words mean
+---------------------------------------------------------------------------------------------+
| Personal (default)                                         38 projects   61 keys             |
|   GitHub        your-user (user)                           30 projects                        |
|   DigitalOcean  team "personal"  via doctl context default  2 apps                            |
|   OpenRouter    account "main"                              9 keys                            |
| Acme (joined, this space is a device of Acme)              12 projects   14 keys   [Open]    |
|   GitHub        acme-inc, acme-labs (GitHub organizations) 12 projects                        |
|   DigitalOcean  team "acme-prod"                            3 apps                            |
|   Cloudflare    account "Acme"                              4 zones                           |
| Client B (local, no server)                                 3 projects    2 keys   [Join...]  |
+---------------------------------------------------------------------------------------------+
Needs a decision (4)
  DigitalOcean team "acme-staging"   suggested: Acme (the hub lists it)        [Acme] [Personal] [Per project]
  GitHub your-user                   holds 2 repositories of acme-inc's product [Decide per project]
  doctl context "old-team"           seen, no token stashed                     [Connect] [Ignore]
  Heroku team "b-apps"               no rule                                    [Client B] [Personal] [Per project]
[Join an organization]   [Add a local organization]
```

| State | What the screen shows |
|---|---|
| Empty | Personal only, with the accounts found; "You are not in any organization. Projects and keys are all personal." and [Join an organization] |
| Loading | the three groups as skeletons of their final size; a provider still scanning shows "reading" on its rows |
| Partial | a provider that could not be read keeps its rows with "not read since TIME" and the reason; others render |
| Error | the organizations file does not read: the screen says which line, keeps everything personal (ORG-R01) and offers the fix |
| Offline | rows from the last scan with their time; join and hub suggestions disabled with "needs the network" |
| Success | as drawn; "Nothing needs a decision" when the list is empty |

### ORG-W2. An account's organization (Observatory; OSS-103)

Opened from a row of ORG-W1. Names the account, what it holds here (projects, apps, zones, keys), who suggested
the mapping (the hub's declared accounts, a repository owner, the person) and the three choices: this
organization, Personal, decide per project. Shows what the choice changes: "12 projects and 3 keys will be
proposed for Acme; nothing is sent". States: loading (counts as skeletons), conflict (two organizations claim it:
both named, the person picks), error (the rules file cannot be written: nothing changed, the reason), success
("Rule saved; 12 projects proposed").

### ORG-W3. Join an organization (Observatory, opened by the Launcher or an invitation link; OSS-104)

```
Join an organization
  Paste the computer token or the invitation link from your organization        [Paste]
  Organization   Acme   (org:acme)   server https://org.example
  Root key       7F3A 19C2 ... 4B10    Compare this with what your administrator sent you
                 [It matches]  [It does not match]
  This computer  macOS user "work"  will be one device of Acme
  Where Acme's work will live   (o) A separate space (recommended)  ( ) This space  ( ) Both here
                                                                               [Join]
```

States: empty (nothing pasted; where to get a token: the hub's Computers page, "Prepare a computer"); checking (the
server is asked for the organization and its root key); mismatch (the fingerprint differs: stop, nothing written,
"Tell your administrator"); expired or used token (the server's words and the step: ask for a new token); offline
("needs the network; nothing was saved"); success (enrolled; next: the notice, ORG-W4).

### ORG-W4. The organization's notice (Hub for the acknowledgement, Observatory shows its state; OSS-104)

Observatory shows the notice version the organization's policy names, what the organization will see and never
see (enrollment, check-ins, the agents summary if you agree, usage of what you share; never personal projects,
keys or files), and whether the person has acknowledged it. The acknowledgement itself happens in the hub's
signed-in page (DEC-0052), which the screen opens. States: not acknowledged ("Nothing about you is collected
until you read it" and [Open the notice]); acknowledged (version and time); a newer version (the old one stays
until the new one is acknowledged); offline (last known state with its time).

### ORG-W5. Choose projects (Observatory, project identities from Fabric's registry; OSS-105)

```
What goes to Acme                                         step 1 of 3: projects
  Selected by your rules (12)                                          [None]
   [x] acme-web         GitHub acme-inc/acme-web       rule: GitHub organization acme-inc
   [x] acme-api         GitHub acme-inc/acme-api       rule: GitHub organization acme-inc
   ...
  Needs you (3)
   [ ] landing-test     GitHub your-user/landing-test  your account; a deploy in team "acme-prod"
   [ ] notes            no repository                  no rule
  Stays personal (38)                                                  [Show]
  Moves out of this space (only when this space becomes Acme's) (5)    [Show]
                                                         [Back]  [Next: keys]
```

States: empty ("No project here matches Acme's accounts" with [Add a rule] and [Choose by hand]); loading; partial
(Fabric's registry or a provider not read: those projects show "account not known" and stay unselected); conflict
(a project claimed by two organizations: ORG-R05); success. Returning: the window opens on the current record with
new proposals marked "new since DATE" (ORG-R16).

### ORG-W6. Choose keys (Observatory; OSS-106)

```
What goes to Acme                                         step 2 of 3: keys
  acme-web
   prod  STRIPE_KEY          issued in: Stripe account "Acme"    -> Acme issues its own; yours is revoked   [change]
   prod  OPENROUTER_API_KEY  issued in: OpenRouter "main" (yours) -> ambiguous: your account, Acme's project
                              ( ) Ask Acme for its own key, keep mine personal
                              ( ) Ask Acme for its own key, revoke mine
                              ( ) Keep personal, do not send
   stage DATABASE_URL        same value as personal/notes/stage/DATABASE_URL -> must be split (two keys)
   prod  SENTRY_DSN          Acme already has prod/SENTRY_DSN (same value) -> nothing to do
  Not movable (2): door admin keys stay with their space                              [Why]
                                                         [Back]  [Next: review]
```

Each key shows its issuing account, the proposal and why (ORG-R06), and three choices: ask the organization for
its own key and revoke mine; ask for its own key and keep mine personal; keep personal and send nothing. A group
action applies one choice to a project's keys or to all keys of one account, listing them first. States: loading
(slot names first, then provenance); partial (an issuer unknown: "put by hand", proposal from the project only);
conflict (ORG-R10); duplicate (ORG-R09); error (the vault does not read: nothing listed as movable); success.

### ORG-W7. Review and confirm (Observatory, the confirmation finished in the Hub; OSS-105, OSS-106)

```
Review                                                     step 3 of 3
  Acme will receive
    12 projects          by an opaque reference; the 11 in Acme's GitHub by repository name
     4 key requests      provider, scopes, environment and slot name; no value
  On this computer
     a new space "acme" (macOS user "work-acme")   needs the administrator password once
     4 keys rotate: Acme's replacements arrive, then 3 of yours are revoked
  Stays personal and is never sent: 38 projects, 57 keys, 5 accounts
  Recorded as: org-selection/1, revision 1, decided by you, 2026-11-08 10:31 UTC
                                                     [Back]  [Confirm with your passkey]
```

Confirm signs the record with the enrollment's key and opens the hub's page where the person confirms it with
their passkey. States: a record changed since the preview (refused, ORG-R14, "Review again"); waiting for the
passkey (the hub page open; nothing applied); offline (the record is saved; "Will send when online"); success
(ORG-W8).

### ORG-W8. Progress and result (Observatory; OSS-105 to OSS-107)

Per item: planned, done, failed (with its step and command), waiting to send. Counts per kind at the top
(ORG-R25). [Undo] while nothing was sent; after that [Return a project] (ORG-W9). An interrupted apply resumes
from the journal when the screen opens again.

### ORG-W9. Acme (Observatory; OSS-107, OSS-108)

The organization's page: certificate and its state (OSS-74), the notice version, the agents summary consent
(OSS-75), the layout, the selection's current revision and its history, [Change the selection], [Return a
project to personal], [Leave Acme]. Removed or closed: the date and the server's words, and the leave plan
already applied (ORG-R17).

### Hub: Computers («Компьютеры») and People

The administrator invites the person (People: one link, single use, 72 hours, sent through the organization's
own mail) and prepares each computer (Computers: "Prepare a computer", a token shown once, confirmed with a
passkey; "Enroll a fleet" for managed machines). The Computers list shows each device, one per OS account or VM,
with its state; "Revoke computer" ends it (passkey, irreversible), which this machine reads as `removed`
(OSS-74, OSS-108).

### Launcher

`passioncode setup` installs Observatory, Fabric and the agents in a space. Its organization join step takes the
token or invitation and hands it to ORG-W3 (or `full org join` without a window). Creating a new OS account for a
separate space is the launcher's administrator step, with the host-label secret (OBS-383) and the keeper for a
space nobody logs in to (OBS-361).

## Agent parity

Every action above has a CLI command and an MCP tool (ORG-R23). An agent runs them on the person's word, with the
words recorded as the decision; the person presses nothing except the gates ORG-R23 names.

| Screen action | CLI | MCP tool | Row |
|---|---|---|---|
| list organizations and accounts | `full org accounts` | `observatory_org_accounts` | OBS-411 |
| map an account | `full org rules set --account KIND:ID --org NAME` (`--per-project`, `--personal`) | `observatory_org_rules` | OBS-412 |
| link a local organization to its enrollment | `full org link NAME --org ID` | `observatory_org_rules` | OBS-413 |
| join | `full org join --from-stdin` (the token or link on stdin) | `observatory_org_join` | OBS-414 |
| notice state | `full org status --org ID` (`notice`) | `observatory_org_status` | OBS-414 |
| classify keys | `full org keys --org ID` | `observatory_org_keys` | OBS-415 |
| hand a key over | part of the selection apply | `observatory_org_selection` | OBS-416 |
| choose, preview, apply, undo | `full org select --org ID plan / show / apply / undo` | `observatory_org_selection` | OBS-417 |
| return a project, leave | `full org unassign PROJECT --org ID`, `full org leave --org ID --plan` then `--apply` | `observatory_org_leave` | OBS-419 |
| everything above as tools | | the tools above | OBS-420 |

## Gap map

Each scenario step and what it needs. "Exists" names what is built today; a row in another product's board is
named by its owner.

| Scenario step | Needs | Exists | Row |
|---|---|---|---|
| OSS-102: accounts discovered and grouped | every provider account with its organization and source; unconnected accounts (a `doctl` context with no stash, a GitHub owner absent from `ownership.json`) | `scan_github` owners, `digitalocean.py teams`, `cloudflare.py list`, `heroku.py teams`, `openrouter.py list`, each separately | **OBS-411** |
| OSS-102: Projects and Home grouped by organization, then account | the grouping in the dashboard | Home groups by organization (OBS-261); Projects groups by first GitHub owner | **OBS-418** |
| OSS-102: one name for a local and a joined organization | an `organizations.json` entry linked to its enrollment and context | two separate things today | **OBS-413** |
| OSS-103: map DigitalOcean teams, Cloudflare accounts, Heroku teams, OpenRouter accounts | rule kinds beyond repository owners and products; suggestions from the hub's declared accounts | `match.repository_owners`, `match.products` | **OBS-412** |
| OSS-104: invitation and computer token | the hub's invitation and Computers page | the hub (built); an agent door for tokens is a hub proposal (VES-ENROLL-PROPOSE) | hub |
| OSS-104: join from a token or link | `full org join`: token to the vault on stdin, root fingerprint check, notice state, enroll | `full org enroll --token-slot` (OBS-161, done) | **OBS-414** |
| OSS-104: the join screen | the desktop window | not built | OBS-200 |
| OSS-104: the notice and its acknowledgement | the hub's signed-in acknowledgement; Observatory reads the version | `purposeHolds`, `noticeVersion` in `org status` (OSS-74) | hub; OBS-414 |
| OSS-105: projects proposed and chosen | the selection window over Fabric's project registry | not built | OBS-232; Fabric's organization slice (F-8) |
| OSS-105, OSS-106: one signed record, preview, apply, undo | `org-selection/1` and its journal | not built | **OBS-417** |
| OSS-105: the hub accepts projects on the person's passkey | the hub side of R1-D12 | hub | hub |
| OSS-106: keys classified | `full org keys` with reasons, duplicates, conflicts | vault metadata, door records, vault digests (OBS-194 step 1) | **OBS-415** |
| OSS-106: a key handed over by rotation | the credential request, the organization's replacement, the revocation | issued keys with provenance (OBS-360, open); credential envelopes (DEC-0111); door `rotate` and `revoke` | **OBS-416**, OBS-360 |
| OSS-106: environments | each slot's environment checked against the organization's | `PROJECT/ENV/NAME` slots; the declaration is not built | OBS-234 |
| OSS-106: a value in two contexts | a finding and its rotation | vault digests per machine (OBS-194 step 1) | **OBS-415**, **OBS-416** |
| OSS-107: a separate space | create, bind, export, import, forget | not built | OBS-362, OBS-359, OBS-364, OBS-372 |
| OSS-107: a space with nobody logged in, a computer with several accounts | per-account ports, jobs, keeper | partly built | OBS-354 to OBS-357, OBS-361 |
| OSS-107: per-context sessions | Switchboard applies the context's profile | not built | OBS-236 |
| OSS-107: the organization's encrypted store | the store profile, then the backup | contract CT-140 open (waits on F-12) | OBS-233 |
| OSS-108: removed, closed, expired | the enrollment ends and says so | built (OSS-74) | OBS-161 |
| OSS-108: return a project, the leave plan | `org unassign` and a leave plan per ORG-R17 | `org leave` removes the enrollment state only | **OBS-419** |
| OSS-108: revoke a computer | the hub's Computers page | the hub (built) | hub |
| OSS-109: every action as a tool | MCP tools and the parity test | the dashboard button inventory (OBS-385) | **OBS-420**, OBS-385 |
| all: who signs the organization record | a decision with Fabric and the hub | open | OBS-380 |

## Decisions taken here

The operator's rules let an agent decide an open question by the recorded decisions, then the project's goal,
then the safest reversible option. These were decided that way; each can be overturned by a later instruction.

1. **One vocabulary: account, organization, context, space**, with "local" and "joined" organizations under one
   word, rather than renaming today's `organizations.json` entries to "owners". Why: the file already means "who a
   project's accounts belong to", which is what a joined organization is too; one word with two badges is fewer
   names for the person than two words.
2. **Keys move only by rotation, never by copy.** Why: the estate review of 2026-11-06 recorded it with the
   contract, Observatory and the hub, and the North Star's first principle (a person's keys never serve the
   organization by accident) needs it.
3. **A separate space is recommended, the other two layouts allowed.** Why: the organization's main Observatory
   lives in its own space (decision of 2026-11-06) and every OS account is a device (2026-11-08), while the
   studio plan of 2026-11-04 calls one space per user a layout, not a product rule.
4. **The organization receives opaque references, not names, except a repository already in one of its
   accounts.** Why: the hub keeps only opaque references to chosen projects (its adaptation design), and a
   repository in the organization's own GitHub tells it nothing new.
5. **The notice is acknowledged in the hub, not in Observatory.** Why: the contract takes the acknowledgement from
   the person's signed-in session, never from a request body (DEC-0052). The OBS-200 row named DEC-0031 for the
   notice; DEC-0031 is the enrollment, and the row now says so.
6. **One context per project until CT-141 decides otherwise.** Why: it is the contract's rule today; a project two
   organizations claim waits for the person.

## Open forks this design waits on (not the operator's)

- F-11 (CT-141): may one project be in several organizations. ORG-R05 holds until it is answered.
- F-12 (CT-140): who decrypts the organization's store. OBS-233 waits on it.
- The agents summary: R1-D17 says a person cannot hide the organization-context summary in mode C, while the
  engine asks consent per organization (OSS-75). The engine keeps the consent step until the contract closes
  CT-92's second half.
