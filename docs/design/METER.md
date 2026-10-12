# The meter: grant checks against delegation tokens (OBS-55)

Observatory is the estate's **meter** in Fabric's delegation model
([Fabric ADR-0144](https://github.com/passioncode-ai/fabric/blob/812876cf/docs/adr/0144-one-delegation-model-fabric-issues-the-chain-the-callee-checks-it-observatory-meters-it.md),
step 3 of [its plan](https://github.com/passioncode-ai/fabric/blob/812876cf/docs/evidence/plans/2026-10-11-delegation-model/README.md)).
Fabric's hub issues identity and holds the grants. Observatory issues neither: it answers the contract's
`grant-check` (fabric-agent-contract DEC-0037/0038/0039, `docs/specification/spend-policy.md` and
`delegation.md` at `8e49fbd`). Code: `observatory/engine/delegation.py` (the verifier),
`observatory/engine/meter.py` (the check and the ledger), `tools/grant.py` (the CLI), `grant.check` in
`mcp/capability_tools.py`, and the door in `mcp/http_service.py`.

## M0 — Trust tiers: proof starts at the machine boundary

The operator's decision of 2026-10-11 (Fabric ADR-0144 amendment 4, `19ad05ca`; the operator's global rule
"This machine is the operator's workplace"):

| Tier | Caller | What a call needs |
|---|---|---|
| T0 | an agent on the operator's own machine, under the operator's account | nothing beyond the operator's instruction: no delegation token, no approval document, no grant record |
| T1 | an agent on another machine of the operator's, enrolled in the agent network (RM-21) | the same as T0, once the machine is enrolled |
| T2 | an agent of another person or organization | the full model: a share grant with limits, an expiry and a payer; a delegation token per call; an approval per effect |

Money limits bind at every tier: the project limit, L0 and each key's ceiling. The human gates bind too:
sign-ins, consents, payments, secrets typed into hidden prompts.

So on the operator's machines the meter and the doors never demand a token. They verify one when it is
presented, for attribution and narrowing. `slotsRequireGrant` stays `when-token` and
`keyserverRequiresDelegation` stays `off` (M5, M6). The strict settings (`always`, `agents`) are for a machine
that serves T2 callers. Observatory's services listen on loopback only, so every caller they see today is T0.

## M1 — Who may ask

A callee about to spend exchanges its own delegation token at the hub for an **onward token for the meter**:
`aud` = `project-observatory`, `scope` = `grant.check`, `effect` = `["none"]`, with the same grant, person and
project. It calls `grant.check` on the memory-http door (`project-observatory full memory-http`, default port
47313) with that token in `Authorization: Bearer`, or the hub forwards it in `Fabric-Delegation`.

`delegation.py` checks the token offline against the estate's Ed25519 keys, in the order of the contract's
verifier table DL-13: the key set, the compact form and header (no duplicate member, `typ`
`fabric-delegation+jwt`, no `jku`/`jwk`/`x5u`/`x5c`/`crit`), the key, the signature, the claims and issuer, the
lifetime (at most 600 s) and clock (60 s leeway both ways), the audience, the chain depth (at most four), the
scope and the effect at a read-only door. Any refusal of the meter's token is `principal-unproven`, with the
verifier's code in the reason (`verifyMeterToken`).

**The token travels in a header and nowhere else** (DL-11). It opens `grant.check` alone: the door puts it on
a `delegation` channel that every memory tool refuses (`unknown-channel`) and every other tool answers
`local-only`. It is never journalled. A present but invalid `Fabric-Delegation` header is the token, never a
fallthrough to a binding. The stdio server carries no token, so `grant.check` there answers
`principal-unproven`. From a terminal: `full grant --check --amount USD --jti JTI < token-file`.

## M2 — Which grant

The token's `grant` claim names `<meta.id>#<meta.revision>`; a token naming several needs `grantRef`, and a
`grantRef` the verified token does not name is `no-grant` (the request is unsigned). The meter reads
`<grantsDir>/<meta.id>.json`, which the hub writes for every grant it holds, in the contract's standing-grant
shape, 0600 in a 0700 directory (Fabric ADR-0144 amendment 1). The read is the contract's `readProjectedGrant`
(DEC-0041 DL-23/DL-24, fabric-agent-contract `7931c1d`), in its order. The file is never authority by itself:

| What the meter finds | Answer |
|---|---|
| no grant directory configured | `unknown-state` (an unknown grant state is a denial) |
| a reference that is not `<id>#<revision>` (the id has no separator and no leading dot) | `no-grant` |
| Windows | `unknown-state`: the profile is POSIX-only in 0.1 |
| no directory, or one not the owner's alone (any group or other bit, or another owner) | `unknown-state` |
| no file | `no-grant` |
| a link, a FIFO or anything but a regular file (one `open` with `O_NOFOLLOW` and `O_NONBLOCK`), a loose or foreign file, over 1 MiB, not JSON, outside the standing-grant shape, times that are not RFC 3339 with a zone, or another grant's content | `unknown-state` |
| an earlier revision than the token names (the hub has not written it yet) | `unknown-state` |
| `state: revoked` / `expired` / any other state but `active` | `grant-revoked` / `grant-expired` / `unknown-state` |
| a later revision than the token names (revised since the token was minted) | `no-grant` |
| past `expiresAt` / before `startsAt` | `grant-expired` / `no-grant` |
| a project other than the token's | `no-grant` |
| no `spend` block and an amount above zero | `no-grant` |

Another agent's key as a credential source is outside the standing-grant shape (DEC-0038), so such a projection
is `unknown-state` before the source is read; `credential-source-refused` stays as a second guard.

## M3 — Windows and levels

The amount is reserved against every window of the grant and every level above it, in one SQLite
`BEGIN IMMEDIATE` transaction, so concurrent checks queue and cannot together overrun a limit:

| Level | Window | Counted over |
|---|---|---|
| L3 | `call` | the grant's `perCallMaxUsd` (contract DEC-0041 adds `call` to the window enum) |
| L2 | `task` | the grant's spend under one task: the token's `task` claim, else the first `act.session` from the outermost actor, else the whole grant (`grant:<ref>`, one window, closed). Never the request's `tokenJti`: it is not signed, so a caller choosing a fresh one per call would open a fresh window and walk past L2 (Fabric ADR-0144 amendment 2, contract DEC-0041) |
| L1 | `total`, `project`, `day`, `week`, `month` | the grant's whole life, everything on its project, and calendar windows in UTC (a week starts on Monday) |
| L0 | `month` | the estate's total across every provider key (`estateMonthUsd`, default USD 1,000) and the ceiling of the provider key the grant's `project-key` source names (`keyCeilingsUsd`) |

**What L0 counts.** `collectors/scan_spend.py` (step `scan-spend`, integration `openrouter`, every tick) sums this
month's `GET /api/v1/activity` for every OpenRouter account the door holds a provisioning key for, into
`store/raw/spend.json`. It records each account's month in USD and the last day covered, and never a key. With a
scan of this month no older than 36 hours, the estate total is what the providers charged plus the reservations
still open; without one, it is the meter's own ledger. `full grant --status` names which (`estateCountedFrom`).
The finding `spend.estate_month` is a warning at the alert ratio (80 %) and critical at the limit (OBS-56).

A reservation counts its amount until `expiresAt` (default one hour later); a settled receipt counts what was
spent instead (`full grant --settle RECEIPT --usd USD`); an unsettled one lapses. `remainingUsd` is the smallest
remainder over the grant's windows, `null` for an unlimited grant. `full grant --status` reports the month
against L0 with `alert` at 80 %, each key against its ceiling, the open reservations, and `degraded` while the
keys or the grant directory cannot be read.

The ledger is `STATE/meter/meter.db` (directory 0700, file 0600). It records the grant reference, the project,
the task key, the key reference, the token's jti, the amount and the times. **It records no person and no
chain** (DL-22): the mapping from a grant to who acted is in the hub's journal.

## M4 — Configuration

`config/meter.json`, every field optional:

| Field | Default | Meaning |
|---|---|---|
| `audience` | `project-observatory` | the meter's agent id: the onward token's `aud` |
| `capability` | `grant.check` | the scope the onward token must carry |
| `issuer` | none (any) | the `iss` to trust (`urn:fabric:estate:<id>`) |
| `hubJson` | none | the hub's `hub.json`: its `jwks` and `grantsDir` |
| `jwks`, `grantsDir` | from `hubJson` | a JWK Set file and the grant directory, overriding `hubJson` |
| `estateMonthUsd` | 1000 | L0, the estate's monthly total |
| `alertRatio` | 0.8 | where `--status` raises `alert` |
| `keyCeilingsUsd` | `{}` | L0 per provider key, by the `ref` of a `project-key` credential source |
| `reservationSeconds` | 3600 | how long an unsettled reservation holds |

The keys are cached for ten minutes and re-read at most once a minute on an unknown `kid` (DL-15).

## M5 — Vault slots under a grant (OBS-57)

`use_secret.py run`, `serve` and `header` release a vault slot into a child process. Under Fabric ADR-0144
amendment 2, a release is authorized by a grant, not by being a local process:

- the agent that uses the slot asks Fabric's hub for a token with `aud` = `project-observatory`, `scope` =
  `secret.use` and `effect` = `none`, under its own grant that names the slot. An agent working for a caller
  mints an onward token from the caller's token instead, so the chain is kept;
- it hands the token over in the environment variable `FABRIC_DELEGATION_TOKEN`. That is never argv and never
  a file. `use_secret.py` removes the variable before anything runs, so no child inherits it, and never logs it;
- the slot `<project>/<env>/<NAME>` is released only when a grant the token names (read from the hub's
  projection as in M2) has `spend.credentialSource` = `{kind: project-key, ref: "<project>/<env>/<NAME>"}`.

Refusals:

| Case | Refusal |
|---|---|
| a `capability` source | `credential-source-refused`: call that agent's capability instead; it never releases a slot |
| a grant naming another slot | `no-grant` |
| an unproven token | `principal-unproven` |
| a revoked or expired grant | `grant-revoked` / `grant-expired` |
| an unreadable projection | `unknown-state` |
| any value from a project's `.env` while a token is present | refused: grants name vault slots only |

A release spends nothing. Provider spend is metered through grant checks and usage reports.

`config/meter.json` `slotsRequireGrant` selects the mode:

- `when-token` (the default, and the setting for the operator's own machines): check a token when one is
  present, and release as before when none is (M0);
- `always`: refuse a release without a token — only for a machine that serves T2 callers;
- `off`: never check.

The journal row (`secret-use.jsonl`) carries the verdict, `granted:<grant refs>`, `no-token` or the refusal
code, and never the token.

## M6 — The keyserver trusts the token (OBS-62)

An agent calling a keyserver action presents a Fabric delegation token (aud `project-observatory`). The scope
and effect per action are Fabric's:

| Action | Scope | Effect |
|---|---|---|
| `mint` | `keys.mint` | `charge` |
| `limit`, `disable`, `enable`, `rotate-key` | `keys.limit`, `keys.disable`, `keys.enable`, `keys.rotate` | `change-policy` |
| `revoke` | `keys.revoke` | `delete` |
| `leak`, `annotate` | `ledger.leak`, `ledger.annotate` | `draft` |

With a valid token the journal row names `caller` (the outermost actor), `principal` (`sub`), `chain` and
`grant`, sets `caller_verified: true`, and keeps the self-named header as `label`. A refused token answers the
contract's code and HTTP status (DL-19) before any effect. `reveal` is never served to a token
(`scope-not-granted`): a value must not enter an agent's context, so agents use `use_secret.py run`.

`keyserverRequiresDelegation` in `config/meter.json` selects the mode:

- `off`: the default, and the setting for the operator's own machines (M0);
- `agents`: a request without this server's `Origin` must carry a token — only for a machine that serves T2
  callers.

**The boundary:** any local process can send an `Origin` header and read the 0600 keyserver token. `agents`
stops accidental and confused borrowing, not a hostile process of the same user (Fabric CO-090).

## Not decided here

- Settling receipts from usage reports (`byGrant`) instead of by hand; other providers' spend in L0 (only
  OpenRouter accounts are read today).
- How an enrolled machine (T1) proves itself to this one: the agent network's device binding (RM-21, OBS-77).
