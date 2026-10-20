# The meter: grant checks against delegation tokens (OBS-55)

Observatory is the estate's **meter** in Fabric's delegation model
([Fabric ADR-0144](https://github.com/passioncode-ai/fabric/blob/812876cf/docs/adr/0144-one-delegation-model-fabric-issues-the-chain-the-callee-checks-it-observatory-meters-it.md),
step 3 of [its plan](https://github.com/passioncode-ai/fabric/blob/812876cf/docs/evidence/plans/2026-10-11-delegation-model/README.md)).
Fabric's hub issues identity and holds the grants. Observatory issues neither: it answers the contract's
`grant-check` (fabric-agent-contract DEC-0037/0038/0039, `docs/specification/spend-policy.md` and
`delegation.md` at `8e49fbd`; the verifier follows DEC-0045 at `07042181`, OBS-162). Code: `observatory/engine/delegation.py` (the verifier),
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
verifier table DL-13 as DEC-0045 sealed it: the trusted issuer and the key set (row 1: no issuer is
`issuer-unavailable`, `no-trusted-issuer`), the compact form and header (no duplicate member, `typ`
`fabric-delegation+jwt`, no `jku`/`jwk`/`x5u`/`x5c`/`crit`), the key (an unknown `kid` is the token's fault; a
published key of another `use` or `alg`, not 32 bytes, **of small order** (`small-order-key`, CT-29) or carrying a
private member is the issuer's, `issuer-unavailable`), the signature, the claims' types (row 5: actors' agent ids
and session ids, URIs, the `scope` and `jti` grammars, each effect once, exactly one grant, `spend`, `task`, `nbf`)
and then the issuer, the lifetime (at most 600 s) and clock (60 s leeway both ways), the audience, the chain depth
(at most four, five on the meter's own token only), the scope and the effect. The meter's token must carry scope
exactly `grant.check` (`meter-scope-not-exact`) and effect exactly `["none"]` (`meter-effect-not-none`). Any refusal
of the meter's token is `principal-unproven`, with the verifier's code and detail in the reason
(`verifyMeterToken`).

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
| L0 | `month` | the estate's total across every provider key (`estateMonthUsd`, default USD 1,000) and the ceiling of the provider key the grant's `project-key` source names (`keyCeilingsUsd`, kept equal to the ceiling `issue-for-policy` set) |

**What L0 counts.** `collectors/scan_spend.py` (step `scan-spend`, integration `openrouter`, every tick) sums this
month's `GET /api/v1/activity` for every OpenRouter account the door holds a provisioning key for, into
`store/raw/spend.json`. It records each account's month in USD and the last day covered, and never a key. With a
scan of this month no older than 36 hours, the estate total is what the providers charged plus the reservations
still open; without one, it is the meter's own ledger. `full grant --status` names which (`estateCountedFrom`).
The finding `spend.estate_month` is a warning at the alert ratio (80 %) and critical at the limit (OBS-56).

**Each project's spend** (J-11, OBS-93; `schema_version` 2 of the same file). The scan also reads every key of
the door's ledger by its hash (`openrouter.read_activity`, one request per key). Two windows, both of completed
UTC days, since the provider's report holds no today: the current month so far and the last 30 days. A key counts
for the project its ledger row names; every name the registry resolves to one project (its folders, its id, its
name) is one bucket keyed by the registry id (`projects.<id>`: `month_usd`, `last30_usd`, `keys`,
`retired_keys`, `folders`, `through`). A key whose row names no project, and each account's spend on hashes no
row or tombstone holds, are `unattributed`; nothing is attributed from a name.

**A retired key keeps its spend with its project, through a snapshot the provider vouches for.** The provider keeps
no history of a deleted key. Measured on 2026-10-15 with free reads on one door account: `GET
/activity?api_key_hash=` for a hash that never existed answered 404, and so did `GET /keys/{hash}` for a key
revoked earlier. So before every DELETE the door reads what the key spent and keeps it. `revoke`, a completed
`rotate` and `revoke --forget-pending` leave a tombstone in the ledger's `retired` list, holding the name, account,
hash, project, `retired_on`, reason and `deleted`. So does `issue` over a row whose key is gone at the provider
(404 by its hash): `deleted: true`, with the snapshot a failed revoke kept on the row (`retiring_snapshot`), if
any. `issue` over a row whose key is still live under another name is refused: a key that may still spend never
leaves the ledger untracked. Tombstones are kept 62 days; one whose `retired_on` does not parse, or that is not an
object, is never pruned, and neither is one not known to be deleted (`deleted` other than true). Such a tombstone is
read by its hash like a live key, whatever its `retired_on`, and its spend is attributed to its project; its key
gone (404) reads every window null, since when it stopped is unknown. The door writes none today; the scan reads
any it finds this way.

The snapshot (`openrouter.usage_snapshot`) makes two reads. First the activity report: the last 30 completed days,
by day. Then, as the last request before the DELETE, the key's own row: `usage_daily`, `usage_weekly` and
`usage_monthly`, the provider's counters for the current UTC day, the week from Monday and the calendar month.
Today and `read_at` come from that last read, never from an earlier read or a clock taken before a delivery.

The snapshot is **reconciled** only when every clause holds:

- the month's activity days plus `usage_daily` equal `usage_monthly`, within $0.0001, whichever side is higher;
- the week's activity days (from Monday) plus `usage_daily` equal `usage_weekly`, within $0.0001;
- each of the last three completed days (`REPORT_LAG_DAYS`) either falls in the month or the week checked, or was
  reached by the activity report (`activity_through`). The report can lag; a day it may still miss and no counter
  covers cannot be vouched for. That is a two-day lag on the Tuesday a month begins (the 29th falls outside both),
  and Monday the 1st unless the report reaches yesterday.

Then today's spend is `usage_daily`, and every day through the retirement is known. Today is never taken from an
activity row: the report covers completed days, and a partial row for today is not part of either check. Otherwise (`reconciled: false`) only the activity days are kept. That happens when the
activity report has not caught up, the reads straddle midnight, the key spends while a successor is delivered, or
a counter is missing. The scan then reads every window holding a day after the last activity day
(`activity_through`), through the retirement day, as null. A mismatch is never booked as unattributed while the
project reads whole. After its snapshot a deleted key spends nothing.

**How long an unreconciled snapshot keeps a project null.** The counter difference is not booked as a known total
for the gap (operator option R5-5, not taken: it would need the month and the week to agree on which days the gap
holds, across a month boundary too). Instead each window stays null while it holds a gap day: the month window
until the month ends, and the last-30-days window until 30 days after the retirement day. A one-day lag at the
moment of a revoke therefore nulls that project's month figure for the rest of the month.

The same holds for a key idle since before the month and the week began: retired on the Tuesday a month begins,
with no spend since the 20th of the month before and counters that agree exactly, its snapshot does not reconcile
(the 29th is a recent day no counter checks and the report does not reach), so that project's month window reads
null for the whole month, though `usage_monthly` vouched for it. Not nulling a window the month counter alone
reconciled (operator option R6-7) was not taken; the null is the price of never booking a lower figure as whole.

The snapshot is written to the ledger before the DELETE: the row's `retiring_snapshot` for a revoke, and
`predecessor_snapshot` for a rotation. A revoke retried after a failure past the DELETE finds the key gone and
retires it with that snapshot. A rotation writes the predecessor's tombstone right after the DELETE, before the
successor's rename, so a failed rename keeps it (`rotation_status: predecessor-retired-rename-pending`). The minted
successor is written to the row (`successor_pending`) before its delivery, so a failure between the delivery and the
next write cannot leave it live and unknown.

A `rotate` of a row a failure left part done first finishes that rotation, then rotates afresh: the caller asked
for a new value, and finishing an old rotation never replaces the current one (`rotate_one` answers `resumed` and
`replaced`; the command and the key page say "rotated", and `--leaked` hands the settle hint, only for a value
replaced). Finishing it means:

- a predecessor still live (its DELETE failed) is snapshotted, the snapshot written to the row, deleted and
  retired;
- a predecessor already gone (the DELETE went through and its answer was lost) is retired with the
  `predecessor_snapshot` kept before that DELETE;
- a successor whose delivery was never recorded (`successor_pending`) is kept on the row as a stale successor
  (`stale_successors`), read by the spend scan like a live key, and deleted and retired (`reason:
  stale-successor`, its spend snapshotted first) by the fresh rotation once a newer successor holds the slot;
- the successor gets the row's name.

A row whose `predecessor_hash` is its own live hash is refused before any request, naming the repair. A `revoke` of a
row holding a stale successor is refused: `rotate` finishes it first. So is an `issue` over such a row: writing its
pending row would drop a successor the slot may hold, a live key leaving the ledger untracked. The refusal says to
run `rotate NAME` first; `revoke` is no way out today (OBS-119).

A stale successor is retired only after a newer successor is delivered. Its spend is read again whenever the key
still answers: `usage_snapshot` runs, the snapshot is written to the row, then the key is deleted. A snapshot kept
on the row from an earlier run is used only when the key answers 404, since a live key may have spent since that
snapshot was read. A false 404, or spend after a kept snapshot's `read_at` day, is not yet caught (OBS-136). A snapshot that could not be read still lets
the delete proceed. The tombstone then says `usage: unread`, and the
windows the key may have spent in are null.

What made the row's own figure unknown goes into its tombstone: a rotation made before tombstones existed
(`untracked_through`) and a rotation still pending (`predecessor_hash`, its spend unread). Both come from one
function, `openrouter.untracked_through`, which the scan calls for live rows and `_retire` for tombstones. A
pending rotation is subtracted there, since its predecessor is carried as `predecessor_hash`, not as untracked. A revoke therefore never turns a project's
honest null into a whole, lower figure.

The ledger's read-modify-write runs under one lock beside the ledger, across processes and threads (`LOCKED_VERBS`:
`issue`, `issue-for-policy`, `rotate`, `revoke`, `revoke --forget-pending`, `claim`, `limit`, `enable`,
`disable`). Two rotations at once used to drop one's successor. A second operation waits up to 150 s, longer than a
rotation whose delivery takes its full minute, so a mint from the key page waits rather than fails; the refusal
names the wait and every locked verb. The key server (`tools/keyserver.py`) answers each request on its own
thread, so a request waiting for the lock does not hold the page; its module keeps no mutable state, and its
audit log is appended with `O_APPEND`. Its one other read-modify-write, a credential's annotation
(`tools/sign_credential.py`), holds its own lock beside the annotations file from read to write. A `retired` that is not a list is refused before any door acts. Windows CI
does not run the door suites. Its job runs the three ledger-lock tests of `tests/test_spend.py` as an
informational step: like every step of that job, it continues on error, and the outcome is written to the job
summary.

A window not read whole is `null`, never a lower figure, with the key named in `unread`:

- a key the provider would not answer for, or answered with rows that are not activity;
- a pending row;
- a tombstone without a snapshot (every window starting on or before its day), or whose snapshot did not reconcile
  (the windows holding a day after its last activity day, through the retirement day);
- a tombstone not known to be deleted whose key is gone (every window), or carrying its row's untracked rotation or
  pending predecessor (the windows starting on or before that day);
- two ledger entries holding the same hash;
- a rotation made before tombstones existed: the row's `rotations` exceed `tombstoned_rotations`, and every window
  starting on or before `tombstones_since` (the day of the last untracked rotation) or `rotated_on` is null;
- the keys not reached when the step's deadline (`OBSERVATORY_STEP_DEADLINE`) arrives or the provider becomes
  unreachable: the scan stops reading and still writes the file. Each account read's timeout is cut to the time
  left less 15 s.

A registry that will not read nulls every project, since which names are one project is unknown. A ledger entry
that is not an object nulls every project and the unattributed total, since it may be any project's key. The
top-level `partial` stays about the estate total (an account not read), so `spend.estate_month` names only
account sources; `attribution_partial` and each `degraded` entry's `scope` (`account` or `attribution`) say the
rest. `tools/spend.py project [PROJECT] --json` answers from the file without asking the provider (`monthUsd`,
`last30Usd`, `partial`, `unread`, `through`, `fresh` by the same 36-hour rule): it is the figure the contract's
launcher reads as a project's recent spend (DEC-0037). A per-project limit is not enforced yet: the finding waits
on a readable recorded spend policy.

A reservation counts its amount until `expiresAt` (default one hour later); a settled receipt counts what was
spent instead (`full grant --settle RECEIPT --usd USD`); an unsettled one lapses. `remainingUsd` is the smallest
remainder over the grant's windows, `null` for an unlimited grant. `full grant --status` reports the month
against L0 with `alert` at 80 %, each key against its ceiling, the open reservations, and `degraded` while the
keys or the grant directory cannot be read.

The ledger is `STATE/meter/meter.db` (directory 0700, file 0600). It records the grant reference, the project,
the task key, the key reference, the token's jti, the amount and the times. **It records no person and no
chain** (DL-22): the mapping from a grant to who acted is in the hub's journal.

## M4 — Configuration

`config/meter.json`; every field is optional except that the meter needs a trusted issuer from one of two places:

| Field | Default | Meaning |
|---|---|---|
| `audience` | `project-observatory` | the meter's agent id: the onward token's `aud` |
| `capability` | `grant.check` | the scope the onward token must carry |
| `issuer` | the hub's, else none | the `iss` to trust (`urn:fabric:estate:<id>`). Without it the meter takes the `issuer` that `hubJson` names, only when the keys come from that same file (no `jwks` override). With neither, every token is refused (`issuer-unavailable`, `no-trusted-issuer`): the contract defines no default, and "any key in the set" is not one |
| `hubJson` | none | the hub's `hub.json`: its `issuer`, `jwks` and `grantsDir` |
| `jwks`, `grantsDir` | from `hubJson` | a JWK Set file and the grant directory, overriding `hubJson` |
| `estateMonthUsd` | 1000 | L0, the estate's monthly total |
| `alertRatio` | 0.8 | where `--status` raises `alert` |
| `keyCeilingsUsd` | `{}` | L0 per provider key, by the `ref` of a `project-key` credential source. Configured, not read from the provider: after `openrouter.py issue-for-policy` moves a key's ceiling, set its entry to the same amount (the plan prints the values it implies) |
| `reservationSeconds` | 3600 | how long an unsettled reservation holds |

The keys are cached for ten minutes and re-read at most once a minute on an unknown `kid` (DL-15).

One deliberate difference from the contract's Node verifier: a published key whose `x` carries a character outside
base64url (a trailing newline, padding) is refused as unusable (`issuer-unavailable`), where Node's decoder skips
it. The difference only refuses; it never accepts a key the contract refuses.

`full doctor` reports the meter's readiness under `meter` without opening its ledger: `issuer` (`config`, `hub` or
`null`), `keys`, `grantDirectory`, `degraded` (`meter:no-trusted-issuer`, `meter:no-estate-keys`,
`meter:no-grant-directory`) and `next`, the one line that fixes the first gap. `full grant --status` carries the
same `degraded` codes.

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
