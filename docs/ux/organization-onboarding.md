# Organization onboarding: from personal to an organization (OSS-102 to OSS-109)

Status: draft, 2026-11-08 (revised the same day after a blind product review). Scenarios: OSS-102 to OSS-109 in
[the full-engine scenario base](../../observatory/engine/docs/ux/portable-scenarios.md#personal-to-organization-onboarding-projects-and-keys-2026-11-08).
Build rows: OBS-411 to OBS-420, OBS-425, OBS-426 and the existing rows named in the [gap map](#gap-map) below, on
[the backlog](../backlog.md). Product outcome: unobserved; nothing here is built yet unless a row says so.

The operator asked on 2026-11-08 how Observatory is set up for a person who will connect to an
organization, and how a personal install converts into an organizational one: the person chooses which
projects and which keys go to the organization, keeps the rest personal, and nothing about it should be
unclear. He also said that GitHub organizations, DigitalOcean teams and Observatory's own "organizations"
read as one blurred grouping. This document is the answer: one vocabulary, the rules that make the
conversion safe, the screens, and what each step needs that does not exist yet.

**Sources this design follows** (read before changing it):

- the organization's first release plan, fabric-workspace `docs/reports/2026-10-25-organization-r1/README.md`
  (onboarding order O-1 to O-4; O-3 is this flow; rows N-14 selection, N-15 encrypted store, N-17
  environments, N-19 a project in several organizations; forks F-11 and F-12, both open; TP-04, Fabric's local
  adaptation plan with its preview and apply journal);
- the estate review of 2026-11-06 (fabric-workspace `docs/plans/2026-11-06-estate-flows/README.md`): a key a
  person holds for an organization's agent is never copied into the organization's vault; the organization
  issues its own key and the old one is revoked, so a hand-over is a rotation; shares and personal snapshots into
  an organization's space are refused. This supersedes O-3(b)'s "the node backs up the selected projects' keys";
- the contract (fabric-agent-contract): contexts are `personal` or `org:<id>`; a context on a project is listed
  under "Not decided here" (`docs/specification/contexts.md`), and CT-141 (a project in several organizations) is
  open; device enrollment DEC-0031; the notice and its acknowledgement from the person's signed-in session, never a
  request body (DEC-0052 item 3); membership DEC-0127 to DEC-0129: the person admits an organization by pinning its
  root in their estate record, signed by their passkey, after comparing its fingerprint (OM-2), leaving is an unpin
  (OM-5), and the OM-7 allowlist of what may leave a person's Fabric (enrollment, check-ins, sessions in the
  organization's context, `grant.usage`, the node summary, credential requests and the chosen adaptation
  references); the adaptation plan `org-adaptation/1` (DEC-0138, AD-1 to AD-6): items with refs random per
  organization and never derived from the estate, labels typed by the person, sent by the person's Fabric with the
  credential the server issued the person; an organization's credential sealed to the enrollment (DEC-0111); a
  device is one counted OS account or one VM (DEC-0154);
- fabric-workspace `docs/reports/2026-10-12-org-contexts/README.md`, invariant I-1: a project carries exactly one
  context. This design assumes I-1; the contract is silent on it;
- the operator's decision relayed on 2026-11-08: every macOS user and every virtual machine is a separate device;
- the hub: an invitation registers the person's passkey in the hub, it does not enroll a computer; the Computers
  page («Компьютеры») prepares one computer's enrollment token, enrolls a fleet, lists computers with their state
  and revokes one; the hub stores provider accounts by name, owner and environments, never keys.

## Four words

The confusion the operator named comes from one word doing four jobs. Every screen, command and
document of this flow uses these four, and the Organizations page carries this table as its help.

| Word | What it is | Who decides it | Examples |
|---|---|---|---|
| **Account** | A container at a provider where resources and keys live. Measured, never invented. | the provider | a GitHub owner (a user or a GitHub organization), a DigitalOcean team (each `doctl` context points at one), a Cloudflare account, a Heroku team (or a user's own apps), an OpenRouter account (one provisioning key) |
| **Organization** | Who a project, its accounts and its keys belong to in Observatory. Always one named **Personal**, the default. Any other is **local** (a name you gave, to keep a client's or an employer's accounts apart; no server) or **joined** (the same name linked to an organization a space of yours is enrolled with). Beside them, **Not ours**: a project every one of whose repository owners is outside your accounts (a clone of somebody else's code). | the person, from rules they confirmed | Personal; Acme (joined); Client B (local); Not ours |
| **Context** | The runtime boundary keys, enrollments and agent instances live in: `personal` or `org:<id>` (the contract's word). A joined organization is a context; a local one lives inside `personal`. | the contract | `personal`, `org:acme` |
| **Space** | One Observatory workspace, normally one OS account or one VM, which an organization counts as one device. A space may be bound to one context. | the person (or the machine's administrator) | the person's own macOS user; a second macOS user kept for Acme's work |

What follows from the table, and what the help says in one line each:

- **A GitHub organization is an account, not an Observatory organization.** Three GitHub organizations,
  two DigitalOcean teams and one Cloudflare account can all belong to Acme. One GitHub user account can hold
  both personal and Acme repositories; then its projects are decided one by one.
- **A DigitalOcean team is an account too.** A `doctl` context is how this machine reaches one team; a context
  with no stashed token is an account seen but not connected.
- **The grouping is Organization, then Account, then Project**, everywhere: Home, Projects, Keys. Today the
  Projects page shows a project's GitHub owner when it has exactly one, and "multi" otherwise
  (`dashboard/build_dashboard.py`, the `owner` column); OBS-418 replaces that with the organization and account
  levels, as one more choice of the group-by control OBS-270 adds (organization, account, label, none).
- **Two files hold this today, under confusing names.** `config/ownership.json` lists which GitHub owners are yours
  under the keys `organizations` and `work_organizations`: those are accounts, not organizations.
  `config/organizations.json` holds organizations in this document's sense (the entry marked `default` is Personal)
  with match rules on repository owners and products only. OBS-412 renames what the person reads (the keys are read
  under their old names too), adds the other account kinds and rewrites ONBOARDING's "Organizations and resources"
  section in the same change; OBS-413 links an entry to its enrollment, so a local organization becomes a joined one
  without a second name.

**Today's assignment, kept.** `organizations.py` answers in this order: declared (a project's own declaration),
rule (an account or product rule), external (shown as Not ours), default (Personal). Two organizations matching one
project is a conflict that assigns neither and is shown; no `organizations.json` means no organization field at all.
This design keeps all four answers and the conflict for local organizations. What changes is a rule that maps a
project to a **joined** organization: it now only proposes (ORG-R28).

## Principles

1. **Nothing enters a joined organization without a decision.** A rule proposes; the person decides.
2. **The organization learns only what was chosen for it,** in the forms the contract allows (OM-7): an adaptation
   item with a random reference and the label the person typed, a credential request without a value. Personal
   projects, keys, accounts and decisions are never sent, not as names, not as counts.
3. **A key changes context only by rotation.** A key is never copied from one context to another; the
   organization issues its own, and the old value is revoked or stays personal (estate review, 2026-11-06). Since no
   personal value ever reaches the organization, leaving never requires rotating a personal key.
4. **The space that becomes the organization's device is chosen before anything enrolls.**
5. **Every step can be undone, and says how.** The one step that cannot be undone is revoking a personal key at its
   provider; it runs last, only after the replacement works (as the person confirms, ORG-R08), and only when the person chose it.
6. **Every button is also an agent's tool** (contract DEC-0121, the operator's rule of 2026-11-07). An agent does
   each step on the person's word and records it; only the steps that need a person's body stay with them.

## Business rules

Numbered for tests and reviews; a scenario names the rules it exercises. "Item" means a project, a key (a vault
slot) or an account.

| ID | Rule | How it is checked |
|---|---|---|
| ORG-R01 | No project, key or account is in a joined organization without a decision recorded for it (ORG-R11). Local organizations keep today's answers: declared, rule, external (Not ours), default (Personal); a conflict assigns none and is shown; no `organizations.json` means no field. | fixtures: no file, a malformed file, a conflict, an external project, a rule to a joined organization with no decision: the last one reads `proposed`, never the organization |
| ORG-R02 | Nothing enters an organization's context and nothing goes to an organization unless the person chose that item, or chose a group whose member list was shown at the time of the choice. A member added to the group later is proposed, never included. | the local record lists each item it covers; an item absent from it is refused at apply |
| ORG-R03 | What reaches the organization about a project is an `org-adaptation/1` item that the person's Fabric sends with the person's credential (DEC-0138 AD-3): a reference random per organization and never derived from the estate, and a label only if the person typed one (AD-2). A repository name goes only as such a typed label, until a contract row allows more. About a key: a credential request (provider, scopes, environment, slot name), never a value. Observatory signs and sends no project selection. | the item Observatory hands Fabric has no field besides ref, kind and label; a test plants a project name and a repository name and fails if either leaves without being typed as a label |
| ORG-R04 | Each account belongs to at most one organization. An account two organizations claim, or a GitHub user account holding repositories of both, is **shared**: its projects and keys are decided one by one. | rule evaluation reports `conflict` or `shared` and assigns nothing |
| ORG-R05 | A project belongs to one context. This is fabric-workspace's invariant I-1, assumed here; the contract has not decided a context on a project, CT-141 is open, and the operator asked (N-19) for a project in Personal and further organizations. Until CT-141, a project two joined organizations claim waits for the person and is shown as a conflict. | the selection refuses a project chosen for two organizations, naming CT-141 |
| ORG-R06 | A key's proposal combines its project's organization and its issuing account's organization (from the door's record). Both agree: proposed for that organization. They disagree, the account is shared, or the issuer is unknown while the project is the organization's: **ambiguous**, stays where it is until chosen. | `full org keys` over fixtures of each case |
| ORG-R07 | A provider admin or provisioning key a door keeps (the Cloudflare and Heroku admin tokens, the OpenRouter provisioning key, a DigitalOcean team token) is never handed over. The organization's space stashes its own; once it has, a personal copy of a token for an account that belongs to the organization is revoked, not handed over. | the key list marks door admin keys `not movable` with the reason and, for an organization's account, `revoke after the organization's space stashes its own` |
| ORG-R08 | Handing a key over is a rotation: a credential request goes to the organization from the space that will hold the key, the replacement arrives into that space's organization context sealed to its enrollment (DEC-0111, OBS-191 parts 2 and 3) with its provenance (OBS-360), its consumers switch, then the personal value is revoked at the provider with proof, unless the person keeps using it personally. "The replacement works" is a local signal only where both contexts share a space (the journal reads the replacement received and its consumers switched); in the separate-space layout the personal space cannot read the organization's (ORG-R20), so the revocation waits for the person's confirmation that Acme's space works (a relayed step). | a hand-over that would place the same value in two contexts is refused; in a separate-space fixture the revocation stays `waiting for your confirmation` until the person's recorded words arrive |
| ORG-R09 | A value present twice is found inside one space only, by comparing values in memory at classification time; no digest under a key shared between contexts is ever written (two contexts must stay unlinkable, DEC-0128). The person's own vault digests (OBS-194) compare only the person's own personal spaces. A duplicate is a finding until rotated into two values; it is never resolved by deleting one copy silently. | a planted duplicate in a mixed space raises the finding; the org space's files hold no cross-context digest |
| ORG-R10 | A name already present in the target context is never overwritten. Equal values: nothing to do, recorded as already there. Different values: the organization's stays, the personal one stays personal, and the person may request one under another name. | apply over a fixture with a conflicting slot leaves both untouched |
| ORG-R11 | The person's choices are one local record, `org-selection/1`: who decided (the person, or an agent with the person's words, time and place, in DEC-0121's decision shape), when, which space it was made in and which space it serves, each item with its decision and why (the rule, or by hand), and the digest of the record it replaces. Owner-only, never sent, never signed by a device key. It is the preview and journal of what Observatory does on this computer; the project items go to Fabric's adaptation plan (TP-04), which owns their preview and journal there. | the record verifies against its digest; no code path sends it |
| ORG-R12 | Applying a record is idempotent and resumable: each item is `planned`, `done`, `failed` or `undone` in a journal; the same record again changes nothing; an interrupted apply continues where it stopped. | apply twice, kill mid-apply and resume, compare journals |
| ORG-R13 | Undo is exact only before Fabric's estate revision that places a project in the organization's context is signed (the entry, R1-D12): nothing has left and the previous record is restored. After the entry, taking a project back is a new record and a new estate revision that removes the project's entry, drafted by Fabric and signed by the person's own passkey (leaving needs either the person or an administrator: Fabric ADR-0180 decision 2, which amends ADR-0178 point 7); until that revision is signed the project stays in the organization's context and the screen says so; Fabric also withdraws the item from the adaptation plan; keys the organization issued for it are deleted here. A personal key already revoked at its provider cannot be restored; the preview says so before the revocation, and a new personal key is minted if needed. | undo before the entry leaves nothing on the server; after it, a take-back waits as `stays in Acme until your passkey signs` and then produces the withdrawal and local deletions |
| ORG-R14 | The preview lists every item that moves, where it goes, what the organization will see, and one line proving the rest: "N projects and M keys stay personal". Apply is refused for a record the person has not previewed in its current form. | apply of a record whose digest differs from the previewed one is refused |
| ORG-R15 | Default selection: projects proposed for the organization by ORG-R26 are selected; ambiguous, shared-account and Personal projects are not. Keys are selected only when project and issuer agree (ORG-R06). | the window's initial state over the fixture set |
| ORG-R16 | A project or key found after the selection that a confirmed rule maps to a joined organization is proposed as a finding ("1 new project may belong to Acme"), never selected by itself. | a new checkout under an Acme-owned repository raises the finding and changes no record |
| ORG-R17 | Leaving: Fabric drafts the one estate revision that revokes the organization's grants, placements and pin, after withdrawing the adaptation plan, and asks the person's passkey for it (OM-5, AD-6, Fabric ADR-0178 point 7); until it is signed the membership stands. Being removed or the organization closing: the server ends the enrollments and the plan (OSS-74, AD-6), and Fabric still asks the passkey for the unpin. An administrator may release one project by a signed release statement; the person's Fabric stops that project's outflow to the organization on receipt and drafts the revision removing its entry for the person's passkey (Fabric ADR-0180 decision 2). In each enrolled space the enrollment ends (OSS-74) and the organization's credentials are deleted (OM-5). The local organization entry stays, with its rules **paused**: its projects read Personal until the person resumes or removes the rules. No checkout, repository, file or personal key is touched. | the leave plan over the fixture set; checkouts and personal slots unchanged byte for byte; session-start names no Acme account afterwards; the plan reads `waiting for your passkey` until the unpin is applied |
| ORG-R18 | Observatory never acknowledges the organization's notice; the person does, in the hub's signed-in page (DEC-0052). Until then the space is enrolled and checks in, and the organization keeps nothing about the person's work. Observatory shows the notice version and whether it is acknowledged. | a space with no acknowledgement shows `waiting for the notice`; no code path posts an acknowledgement |
| ORG-R19 | No pin without a compared fingerprint (Fabric ADR-0178 point 6): Fabric shows the root's fingerprint, the person compares it with the one in the administrator's invitation (the administrator's own Fabric shows it, and the invitation message carries it), and only a match lets the passkey sign the pin; a mismatch, or an invitation without a fingerprint, pins nothing and says why. Observatory's join enrolls only once the estate revision this space applied pins the organization; until then it waits as `not admitted in your estate yet`. | a join with no pinning revision applied enrolls nothing and reads `not admitted in your estate yet` |
| ORG-R20 | A space bound to a context (OBS-359) refuses items of any other context (the person's estate record, applied read-only, excepted: ORG-R30); a mixed space keeps a context on every project and slot and the organization sees only its own (mode C, DEC-0118). | context check over a bound and a mixed fixture |
| ORG-R21 | A computer token enrolls one space as one device; a second space needs its own token. A fleet token (the hub's "Enroll a fleet", for managed computers) is the administrator's and enrolls many. | a second enroll with a used computer token is refused by the server and named |
| ORG-R22 | A key keeps its environment. Until environments are data (OBS-234) the environments are `local`, `stage` and `prod`; a slot in another environment is shown as such and needs a choice. | classification of a slot in `qa` reads `environment not declared` |
| ORG-R23 | Every action of the screens has a CLI command and an MCP tool with the same effect and the same refusals, and a gate class (none, relayed, human; contract DEC-0121). An agent acting on the person's word records the decision in DEC-0121's shape: `{by: operator, via: relayed-instruction, action, target, instruction (the words verbatim), at, source}`. The human steps are named once each: registering the passkey from the hub's invitation; comparing the root fingerprint and signing the pin with the passkey in Fabric; signing, with the passkey in Fabric, the estate revision that places the chosen projects in the organization's context (R1-D12: a project enters an organization by its person's own passkey, Fabric ADR-0178 point 7 and the contract's membership OM-7); signing, with the passkey in Fabric, a revision that removes a project's entry or ends the membership (Fabric ADR-0180 decision 2, ADR-0178 point 7); acknowledging the notice in the hub; the administrator preparing a computer token for the space and, when the invitation lacks it, sending the root fingerprint (an administrator's steps, possibly another person); the macOS administrator password when a new OS account is created. | the parity test: every screen action maps to a door with its gate class; the gate list is the same in the screen, the CLI help and the tool descriptions |
| ORG-R24 | Offline: accounts, rules, the selection and its preview work without the network; local moves between the person's own spaces work offline; anything for the server waits in its outbox and the screen says "waiting to send", never "done". | the apply journal with the server unreachable |
| ORG-R25 | A partial result is never shown as success: the result screen counts done, failed and waiting per kind, and each failed item names its step and command. | a fixture where one key's provider refuses the revocation |
| ORG-R26 | A project's proposal comes from all its accounts together: its repositories, its deploys (DigitalOcean apps, Heroku apps) and its zones. An account with no rule contributes `none`, never the default organization. All accounts with a rule point to one organization: proposed for it. They point to two organizations, or to an organization and Personal (a repository in Acme's GitHub, its deploy in the person's own team): **ambiguous**, decided by the person. | fixtures of each mix |
| ORG-R27 | A personal key never disappears, and nothing personal stays behind. When a space becomes the organization's, every personal kind is carried into the person's personal space and verified there, or removed with its reason, before the space is bound (OBS-426): vault slots (carried and verified by digest); the vault-digests estate key with `vault-digests/local.json` and `peers/` (removed, since the personal space has its own); the person's SSH keys (never moved: the personal space makes and registers its own key wherever the old one is registered, `full ssh-keys rekey OLD --apply` (OSS-96), then the old key's registrations are removed as a rotation removes them (OSS-67, OBS-112) and its file is removed here); the personal context's device keys under `node/` (removed; the space enrolls in the organization with its computer token, it does not pair again); the person's estate record stays, applied read-only, because the pin and unpin of the organization live there (ORG-R30); shares received (removed, the sender shares again to the personal space); agent memory and ledger records of personal projects (exported, then removed); the workspace's own encrypted snapshot settings (pointed at a new destination; personal snapshots are never restored into it; the estate record kept under ORG-R30 is left out of those snapshots when the destination can reach the organization). A key is never "forgotten"; projects' curated entries may be forgotten only after their import elsewhere is confirmed (OBS-372). | a conversion fixture: every personal slot reads `same` in the personal space before it is gone here, and the bound space holds none of the listed kinds |
| ORG-R28 | Readers of a project's organization (the session-start line naming its analytics account and design team, the analytics findings) read the decided answer, not a bare rule: a rule that maps a project to a joined organization yields `proposed` until the person chose it (`selected`), and a `proposed` project gets no account of that organization in the session-start line or the findings: the line says it is proposed and to ask before creating anything. Local organizations keep today's rule answer. | a rule-mapped, unchosen project: the session-start line names no Acme account and the analytics check skips it |
| ORG-R29 | The layout (where the organization's work lives) is chosen before any enrollment. In the separate-space layout the current space never enrolls in the organization: the join and every key receipt run in the new space; the selection is made in the space that holds the items and names the space it serves. | the separate-space path over a fixture: the personal space has no enrollment for Acme at any step |
| ORG-R30 | A space bound to an organization keeps applying the person's estate revisions and revocation deltas (`full org records apply`, verified, read-only): the pin, the project entries and the unpin live there, so admission (ORG-R19) and the end of a membership (OM-5) are read from them. Its source is the person's own space on this computer, which holds the personal device key that receives the revisions from the relay and hands each verified revision and delta over locally as a file; the bound space applies the file read-only (`full org records apply --record FILE`, `--delta FILE`) and never reaches the relay itself (OBS-426). It holds no personal-context device key or placement; the estate record is the one personal document it keeps, and it is not a key. The record and the members it names are left out of every snapshot, backup or store that can reach the organization (the bound space's snapshots and the organization's encrypted store, OBS-233), since a person's estate record never leaves for an organization (the contract's membership OM-7). | a bound fixture: an unpin applied ends the enrollment's collection; no personal device key is found |

## Where the organization's work lives

The person picks one of three layouts **before** this computer enrolls (ORG-R29); the screen recommends the first
and shows the costs of the others. The organization's own main Observatory lives in its own space (operator
decision of 2026-11-06); joining adds an organization beside `personal` and never replaces it.

| Layout | What happens | When it fits | Costs |
|---|---|---|---|
| **A separate space** (recommended) | A new OS account (or VM) on this computer, or another computer, becomes a space bound to `org:<id>` (OBS-362, OBS-359) and enrolls with its own computer token; the current personal space does not enroll. The selection is made in the personal space (it holds the projects and keys); the chosen projects' curated entries are exported into the new space (OBS-364); the new space requests the organization's keys through its own enrollment (ORG-R08); after the import is confirmed, the personal space forgets the curated entries that moved (OBS-372) and revokes the personal keys the person chose to revoke, each once the person confirms Acme's replacement works (ORG-R08). | the person works for the organization on a machine that is also theirs | a second login; the macOS administrator password once; a second computer token from the administrator |
| **This space is the organization's** | The OS account Observatory runs in is used only for the organization. Every personal kind found in it (keys, digest key and manifests, SSH keys by rekey, personal device keys, shares received, memory, snapshot settings) moves to the person's personal space or is removed first; the person's estate record stays, read-only (ORG-R30) (ORG-R27, OBS-426); personal projects' curated entries are exported there; then the space is bound to `org:<id>` and enrolls. | a work account or a VM made for the organization | personal items must leave first; the selection shows them as "moves out" |
| **Both in one space** | One workspace keeps both contexts and enrolls; every project and slot carries its context; the organization sees only its own (mode C). | a single-account machine where a second login is not possible | the person must keep the contexts apart; the organization's notice may say that two organizations on one computer can be linked (DEC-0154) |

**Machines enrolled before this design** (`full org enroll`, since 0.27.0) keep their enrollment. Once OBS-413 links
the enrollment to its `organizations.json` entry, the organization reads as joined and its layout as "both in one
space". A person who then chooses a separate space enrolls the new space first and runs `full org leave` in the old
one afterwards; nothing was selected under the old enrollment, so nothing moves back.

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
| Acme (joined: space "work-acme" is a device of Acme)       12 projects   14 keys   [Open]    |
|   GitHub        acme-inc, acme-labs (GitHub organizations) 12 projects (10 chosen, 2 proposed)|
|   DigitalOcean  team "acme-prod"                            3 apps                            |
|   Cloudflare    account "Acme"                              4 zones                           |
| Client B (local, no server)                                 3 projects    2 keys   [Join...]  |
| Not ours (clones of other people's code)                    4 projects                        |
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
| Loading | the groups as skeletons of their final size; a provider still scanning shows "reading" on its rows |
| Partial | a provider that could not be read keeps its rows with "not read since TIME" and the reason; others render |
| Error | the organizations file does not read: the screen says which line and offers the fix; projects keep their last emitted answer and none moves into a joined organization (ORG-R01). Today an unreadable file loads as empty and the next rebuild drops every project's organization field; OBS-412 changes that |
| Offline | rows from the last scan with their time; join and hub suggestions disabled with "needs the network" |
| Success | as drawn; "Nothing needs a decision" when the list is empty |

### ORG-W2. An account's organization (Observatory; OSS-103)

Opened from a row of ORG-W1. Names the account, what it holds here (projects, apps, zones, keys), who suggested
the mapping (the hub's declared accounts, a repository owner, the person) and the three choices: this
organization, Personal, decide per project. Shows what the choice changes: "12 projects and 3 keys will be
proposed for Acme; nothing is sent". States: empty (no account needs a decision: "Every account has an
organization"); loading (counts as skeletons); conflict (two organizations claim it: both named, the person picks);
error (the rules file cannot be written: nothing changed, the reason); success ("Rule saved; 12 projects proposed").

### ORG-W3. Join an organization (Observatory, opened by the Launcher; OSS-104)

Shown in the space that will be the device (ORG-R29): in the separate-space layout, in the new OS account.

```
Join an organization
  Organization    Acme   (org:acme)   server https://org.example
  In your estate  admitted 2026-11-08 by your passkey in Fabric       (or: not admitted yet, open Fabric)
  Notice          version 3, acknowledged 2026-11-08 in the hub        (or: not yet, [Open the notice])
  This space      macOS user "work-acme"  will be one device of Acme
  Computer token  [Paste the token from your administrator]   (read once, kept in the vault, never shown)
                                                                               [Join]
```

States: empty (no token: where it comes from, the hub's Computers page, "Prepare a computer", made by an
administrator); no fingerprint (Acme's invitation carries none, so Fabric pins nothing: "Ask your administrator for
the fingerprint their Fabric shows"; the join cannot complete until the pin, ORG-R19); not admitted (the applied
estate revision does not pin Acme: "Admit Acme in Fabric first, with your passkey"); checking (the server asked); refused (token expired, used, for another organization or for a
fleet: the server's words and "ask your administrator for a new computer token"); offline ("needs the network;
nothing saved"); waiting for the notice (enrolled; Acme keeps nothing about your work yet, ORG-W4); success
(enrolled, admitted, notice acknowledged).

### ORG-W4. The organization's notice (Hub for the acknowledgement, Observatory shows its state; OSS-104)

Observatory shows the notice version Acme's policy names, what Acme will see and never see (that this computer is
enrolled and checks in; the agents summary if you agree; usage of what you share; never personal projects, keys or
files), and whether the person has acknowledged it. The acknowledgement itself happens in the hub's signed-in page
(DEC-0052), which the screen opens. States: not acknowledged (the line below, and [Open the notice]); acknowledged
(version and time); a newer version (the old one stays until the new one is acknowledged); offline (last known
state with its time).

Draft string, to go through copywriting before it ships: "Acme keeps nothing about your work until you acknowledge
its notice. It already knows this computer is enrolled."

### ORG-W5. Choose projects (Observatory, project identities from Fabric's registry; OSS-105)

Shown in the space that holds the projects; its header names the space it serves ("for Acme's space work-acme").

```
What goes to Acme                                         step 1 of 3: projects
  Proposed by your rules (12)                                          [None]
   [x] acme-web         GitHub acme-inc/acme-web       rule: GitHub organization acme-inc
       label for Acme:  [____________________]   (what Acme will read; leave empty to send no name)
   [x] acme-api         GitHub acme-inc/acme-api       rule: GitHub organization acme-inc
   ...
  Needs you (3)
   [ ] landing-test     GitHub your-user/landing-test  your repository; its deploy in team "acme-prod"
   [ ] notes            no repository                  no rule
  Stays personal (38)                                                  [Show]
  Moves out of this space (only when this space becomes Acme's) (5)    [Show]
                                                         [Back]  [Next: keys]
```

The label field starts empty: Acme reads a name only if the person types one (ORG-R03). States: empty ("No project
here matches Acme's accounts" with [Add a rule] and [Choose by hand]); loading; partial (Fabric's registry or a
provider not read: those projects show "account not known" and stay unselected); conflict (a project claimed by two
organizations: ORG-R05); success. Returning: the window opens on the current record with new proposals marked
"new since DATE" (ORG-R16).

### ORG-W6. Choose keys (Observatory; OSS-106)

```
What goes to Acme                                         step 2 of 3: keys
  acme-web
   prod  STRIPE_KEY          issued in: Stripe account "Acme"    -> Acme issues its own; yours is revoked   [change]
   prod  OPENROUTER_API_KEY  issued in: OpenRouter "main" (yours) -> ambiguous: your account, Acme's project
                              ( ) Ask Acme for its own key, keep mine personal
                              ( ) Ask Acme for its own key, revoke mine once Acme's works
                              ( ) Keep personal, ask for nothing
   stage DATABASE_URL        same value as notes/stage/DATABASE_URL in this space -> must be split (two keys)
  Not movable (2): door admin keys stay with their space                              [Why]
   DigitalOcean team token "acme-prod"   Acme's account: revoke after Acme's space stashes its own
                                                         [Back]  [Next: review]
```

Each key shows its issuing account, the proposal and why (ORG-R06), and three choices: ask the organization for its
own key and revoke mine once it works; ask for its own key and keep mine personal; keep personal and ask for nothing.
A group action applies one choice to a project's keys or to all keys of one account, listing them first. States:
empty (the chosen projects have no keys here: "Nothing to hand over; Acme's keys for these projects are Acme's to
issue"); loading (slot names first, then provenance); partial (an issuer unknown: "put by hand", proposal from the
project only); conflict (ORG-R10); duplicate (ORG-R09); error (the vault does not read: nothing listed as movable);
success.

### ORG-W7. Review and confirm (Observatory, project items handed to Fabric; OSS-105, OSS-106)

```
Review                                                     step 3 of 3
  Made in: your space "you"     For: Acme's space "work-acme" (to be created)
  Acme will receive, from your Fabric
    12 projects          each as a reference Acme cannot trace back; a name only where you typed one (10)
  Acme's space will ask Acme for
     4 keys              provider, scopes, environment and slot name; no value
  On this computer
     a new space "work-acme" (macOS user)          needs the administrator password once
     a computer token for it                       your administrator prepares it in the hub
     3 of your keys are revoked, each after you confirm Acme's replacement works   (cannot be undone)
  In Fabric
     your passkey signs the revision that places the 12 projects in Acme's context
  Stays personal and is never sent: 38 projects, 57 keys, 5 accounts
  Recorded here as: org-selection/1, revision 1, decided by you, 2026-11-08 10:31 UTC
                                                     [Back]  [Apply]
```

Apply hands the project items to Fabric's adaptation plan (Fabric shows its own preview, asks the person's passkey to
sign the estate revision that places the projects in Acme's context, and sends the plan with the person's credential)
and starts the local journal. States: a record changed since the preview (refused, ORG-R14,
"Review again"); waiting (for the new space, its token, Acme's replacements: each named); offline (the record is
saved; "Will continue when online"); success (ORG-W8).

### ORG-W8. Progress and result (Observatory; OSS-105 to OSS-107)

Per item: planned, done, failed (with its step and command), waiting (and on what). Counts per kind at the top
(ORG-R25). [Undo] until Fabric's entry revision is signed; after that [Take a project back] (ORG-W9), which waits as "stays in Acme until your passkey signs" (ORG-R13). An interrupted apply
resumes from the journal when the screen opens again.

### ORG-W9. Acme (Observatory; OSS-107, OSS-108)

The organization's page: each enrolled space with its certificate and state (OSS-74), the notice version, the agents
summary consent (OSS-75), the layout, the selection's current revision and its history, [Change the selection],
[Take a project back], [Leave Acme]. Removed or closed: the date and the server's words, and the leave plan already
applied (ORG-R17).

### Hub: People, Computers («Компьютеры»), the notice

The administrator invites the person (People: one link, single use, 72 hours, sent through the organization's own
mail); redeeming it registers the person's passkey in the hub (the hub's sign-in by invitation and passkey). The
person acknowledges the notice in the hub. The administrator prepares each computer (Computers: "Prepare a
computer", a token shown once, confirmed with a passkey; "Enroll a fleet" for managed computers). The Computers list
shows each device, one per OS account or VM, with its state; "Revoke computer" ends it (passkey, irreversible), which
the space reads as `removed` (OSS-74, OSS-108).

### Fabric

Joining in Fabric: Fabric reads the organization's root, shows its fingerprint, the person compares it with the one in
the administrator's invitation, and only a match lets their passkey sign the estate revision that pins it (OM-2,
Fabric ADR-0178 point 6). The source is the administrator's own Fabric, which shows the fingerprint, and the
invitation message, which carries it; the hub's invitation today is a link only, so an invitation without a
fingerprint pins nothing and says why, and the join waits (gap map rows for Fabric and the hub). Fabric also asks the
person's passkey to sign the estate revision that places each chosen project in the organization's context (R1-D12,
Fabric ADR-0178 point 7), and the revision that removes a project's entry or ends the membership (Fabric ADR-0180
decision 2).
Fabric owns the adaptation plan (TP-04): its preview, its journal and sending `org-adaptation/1` with the person's
credential; and leaving: withdrawing the plan, then unpinning (OM-5, AD-6).

### Launcher

`passioncode setup` installs Observatory, Fabric and the agents in a space. Creating a new OS account for a separate
space is its administrator step, with the host-label secret (OBS-383) and the keeper for a space nobody logs in to
(OBS-361); it then opens ORG-W3 in that space (or `full org join` without a window).

## Agent parity

Every action above has a CLI command and an MCP tool (ORG-R23). An agent runs them on the person's word and records
the decision in DEC-0121's shape `{by: operator, via: relayed-instruction, action, target, instruction, at, source}`.
Gate: **none** (an agent may run it without the person's words: reads), **relayed** (runs on the person's recorded
words), **human** (a person's body is needed; the agent prepares everything and names the step once).

| Screen action | CLI | MCP tool | Gate | Row |
|---|---|---|---|---|
| list organizations and accounts | `full org accounts` | `observatory_org_accounts` | none | OBS-411 |
| map an account | `full org rules set --account KIND:ID --org NAME` (`--per-project`, `--personal`) | `observatory_org_rules` | relayed | OBS-412 |
| pause, resume or remove a local organization's rules | `full org rules pause, resume, remove` | `observatory_org_rules` | relayed | OBS-412 |
| link a local organization to its enrollment | `full org link NAME --org ID` | `observatory_org_rules` | relayed | OBS-413 |
| choose the layout, create the space | `full space create --context org:ID` | `observatory_org_selection` | relayed; the macOS administrator password is human | OBS-362, OBS-417 |
| join with a computer token | `full org join --org ID` (the token on stdin) | `observatory_org_join` | relayed; the token itself is made by an administrator (human) | OBS-414 |
| admission and notice state | `full org status --org ID` (`admission`, `notice`) | `observatory_org_status` | none; the pin and the acknowledgement are human, in Fabric and the hub | OBS-414 |
| classify keys | `full org keys --org ID` | `observatory_org_keys` | none | OBS-415 |
| choose, preview, apply, undo | `full org select --org ID plan, show, apply, undo` | `observatory_org_selection` | relayed; the passkey signing the projects' estate revision in Fabric is human | OBS-417 |
| hand a key over, revoke the personal value | part of apply; `full org select --org ID confirm-replacement KEY` | `observatory_org_selection` | relayed (the person's confirmation that the replacement works, in the separate-space layout) | OBS-416 |
| move personal keys out of a space becoming the organization's | part of apply | `observatory_org_selection` | relayed | OBS-426 |
| take a project back, leave | `full org unassign PROJECT --org ID`, `full org leave --org ID --plan` then `--apply` | `observatory_org_leave` | relayed; the passkey signing the removal or the unpin in Fabric is human; an administrator's release statement is another person's step | OBS-419 |
| everything above as tools, with the gate column | | the tools above | | OBS-420 |

## Gap map

Each scenario step and what it needs. "Exists" names what is built today; a row in another product's board is
named by its owner.

| Scenario step | Needs | Exists | Row |
|---|---|---|---|
| OSS-102: accounts discovered and grouped | every provider account with its organization and source; unconnected accounts (a `doctl` context with no stash, a GitHub owner absent from `ownership.json`) | `scan_github` owners, `digitalocean.py teams`, `cloudflare.py list`, `heroku.py teams`, `openrouter.py list`, each separately | **OBS-411** |
| OSS-102: Projects and Home grouped by organization, then account | the organization and account levels in the group-by control | Home groups by organization (OBS-261); Projects shows one owner or "multi"; group-by by label is OBS-270 | **OBS-418**, OBS-270 |
| OSS-102: the words people read, and the two files | `ownership.json` and `organizations.json` named for what they hold; ONBOARDING's section rewritten | the files and the section as described above | **OBS-412** |
| OSS-102: one name for a local and a joined organization; machines enrolled since 0.27.0 | an `organizations.json` entry linked to its enrollment and context | two separate things today | **OBS-413** |
| OSS-103: map DigitalOcean teams, Cloudflare accounts, Heroku teams, OpenRouter accounts | rule kinds beyond repository owners and products; project-level evidence (ORG-R26); suggestions from the hub's declared accounts | `match.repository_owners`, `match.products` | **OBS-412** |
| OSS-103, ORG-R28: readers of the organization answer | `proposed` and `selected` sources; the session-start line and the analytics findings read them | `organizations.py` assigns by rule at once; `tools/session_start.py` and `tools/google_findings.py` read that | **OBS-412**, **OBS-413** |
| OSS-104: invitation and passkey | the hub's invitation | the hub (built) | hub |
| OSS-104: admission (fingerprint and pin) | Fabric's join and the passkey's signature over the estate revision (OM-2) | Fabric | Fabric |
| OSS-104: a source for the root fingerprint | the administrator's own Fabric shows the organization's root fingerprint (Fabric ADR-0178 point 6) | not built; without it no pin, and the join waits | Fabric |
| OSS-104: the invitation carries the fingerprint | the hub's invitation message carries the fingerprint the administrator's Fabric shows | the invitation is a link only | hub |
| OSS-105: a project enters by the person's passkey | Fabric asks the passkey to sign the estate revision placing the projects in the organization's context (R1-D12) | Fabric ADR-0178 point 7 | Fabric |
| OSS-108: a project leaves, a membership ends | the person's passkey signs the removal or the unpin; an administrator's release statement takes effect on receipt | Fabric ADR-0180 decision 2 (contract CT-159, open), ADR-0178 point 7 | Fabric |
| OSS-104: the notice acknowledgement | the hub's signed-in page (DEC-0052) | the hub | hub |
| OSS-104: computer token | the hub's Computers page; an agent door for tokens is a hub proposal (VES-ENROLL-PROPOSE) | the hub (built) | hub |
| OSS-104: join from a computer token, admission and notice state | `full org join`: token to the vault on stdin, enroll, read admission and notice | `full org enroll --token-slot` (OBS-161, done) | **OBS-414** |
| OSS-104: the join screen | the desktop window | not built | OBS-200 |
| OSS-105: projects proposed and chosen | the selection window over Fabric's project registry | not built | OBS-232; Fabric's organization slice (F-8) |
| OSS-105: projects enter the organization | `org-adaptation/1` items, preview and journal, sent with the person's credential | contract DEC-0138; the form of the organization part is contract CT-169 | Fabric TP-04, contract CT-169 |
| OSS-105, OSS-106: the local record, preview, apply, undo | `org-selection/1` and its journal, handing project items to Fabric | not built | **OBS-417** |
| OSS-105 to OSS-108: the windows for keys, review, progress and the organization | ORG-W6 to ORG-W9 in the desktop app | not built | **OBS-425** |
| OSS-106: keys classified | `full org keys` with reasons, duplicates inside one space, conflicts | vault metadata, door records | **OBS-415** |
| OSS-106: the request queue and delivery into an organization's context | credential requests, the administrator's decision, envelopes into the context | not built | OBS-191 parts 2 and 3, OBS-360 |
| OSS-106: a key handed over by rotation | switching consumers to the replacement, then a proved revocation of the personal value | door `rotate` and `revoke` | **OBS-416** |
| OSS-106: environments | each slot's environment checked against the organization's | `PROJECT/ENV/NAME` slots; the declaration is not built | OBS-234 |
| OSS-107: a separate space | create, bind, export, import, forget | not built | OBS-362, OBS-359, OBS-364, OBS-372 |
| OSS-107: personal keys out of a space that becomes the organization's | carry each personal slot into the personal space, verify, then delete here | encrypted snapshots carry the store (OBS-350) | **OBS-426** |
| OSS-107: a space with nobody logged in, a computer with several accounts | per-account ports, jobs, keeper | partly built | OBS-354 to OBS-357, OBS-361 |
| OSS-107: per-context sessions | Switchboard applies the context's profile | not built | OBS-236 |
| OSS-107: the organization's encrypted store | the store profile, then the backup of the organization context's slots | contract CT-140 open (waits on F-12) | OBS-233 |
| OSS-108: removed, closed, expired | the enrollment ends and says so | built (OSS-74) | OBS-161 |
| OSS-108: withdrawal and unpin | Fabric withdraws the plan and unpins | Fabric (AD-6, OM-5) | Fabric |
| OSS-108: take a project back, the leave plan | `org unassign` and a leave plan per ORG-R17 | `org leave` removes the enrollment state only | **OBS-419** |
| OSS-108: revoke a computer | the hub's Computers page | the hub (built) | hub |
| OSS-109: every action as a tool, with its gate | MCP tools and the parity test | the dashboard button inventory (OBS-385) | **OBS-420**, OBS-385 |
| all: who signs the organization's estate record | a decision with Fabric and the hub (not the selection: Observatory signs no selection, ORG-R11) | open | OBS-380 |

## Decisions taken here

The operator's rules let an agent decide an open question by the recorded decisions, then the project's goal,
then the safest reversible option. These were decided that way; each can be overturned by a later instruction.

1. **One vocabulary: account, organization, context, space**, with "local" and "joined" organizations under one
   word, and "Not ours" for today's `external`. Why: `organizations.json` already means "who a project's accounts
   belong to", which is what a joined organization is too; one word with badges is fewer names than two.
2. **Keys move only by rotation, never by copy.** Why: the estate review of 2026-11-06 recorded it with the
   contract, Observatory and the hub, and the North Star's first principle (a person's keys never serve the
   organization by accident) needs it.
3. **The layout is chosen first, and a separate space is recommended.** Why: the organization's main Observatory
   lives in its own space (decision of 2026-11-06), every OS account is a device (2026-11-08), and a personal space
   enrolled by mistake would be the organization's device.
4. **Projects enter the organization only through Fabric's adaptation plan.** Why: DEC-0138 (AD-1 to AD-3) fixes
   the wire, refs random per organization (DEC-0128), labels typed by the person; OBS-259 (Observatory signing the
   plan) was cancelled for this reason.
5. **The notice is acknowledged in the hub, not in Observatory.** Why: DEC-0052. The OBS-200 row named DEC-0031 for
   the notice; DEC-0031 is the enrollment, and the row now says so.
6. **One context per project, as an assumption.** Why: fabric-workspace's invariant I-1; the contract leaves it
   undecided and CT-141 is open, so a project two organizations claim waits rather than guessing.
7. **After leaving, the local entry stays with its rules paused.** Why: the projects of a left organization must not
   keep sending agents to its analytics account and design team (ORG-R28); pausing keeps the person's rules for a
   later rejoin.

## Open forks this design waits on (not the operator's)

- F-11 (CT-141, N-19): may one project be in several organizations. ORG-R05 holds until it is answered.
- F-12 (CT-140): who decrypts the organization's store. OBS-233 waits on it.
- The agents summary: R1-D17 says a person cannot hide the organization-context summary in mode C, while the
  engine asks consent per organization (OSS-75). The engine keeps the consent step until the contract closes
  CT-92's second half.
