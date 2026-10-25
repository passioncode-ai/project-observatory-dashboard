# Review policy: proposals decided once, by a rule a person enabled

Decision record for OBS-227, 2026-10-24. The operator asked that the whole queue of proposals be
reviewed, constantly and without being forgotten, preferably with no coding agent running; and, where
a person is needed, that the remainder accumulates and one launch resolves it.

- **Policy and evaluator:** [`review_policy.py`](../../observatory/engine/review_policy.py). One
  evaluator answers the dry run, the tick's run, the batch and the counts; nothing else decides.
- **Doors:** `project-observatory full review policy show|enable|disable|run`,
  `full review batch prepare|apply`, `full review undo-policy` ([`tools/review.py`](../../observatory/engine/tools/review.py)).
- **Tick step:** `review-policy`, an optional tail step after `corroborate` (`tools/tick.py`).
- **Finding:** `review.queue_waiting` ([`tools/review_findings.py`](../../observatory/engine/tools/review_findings.py)).
- **Tests:** [`tests/test_review_policy.py`](../../observatory/engine/tests/test_review_policy.py).

## The two queues

| Queue | What waits | Who decides it today |
|---|---|---|
| Registry proposals (`proposals` table) | `observatory_propose` patches: `resources`, `aliases`, `description`, `organization`, `local_folders` | `review.py accept-proposal` / `reject-proposal`, at a terminal |
| Ledger conclusions (`ledger`, state `proposed`) | what agents and the observer concluded | a person (`review.py promote` / `reject` / `reject-group`), a second witness (`corroborate`), or retention after 90 days (365 for a lesson) |

Nothing here changes rule 3 of the project: **a model may only propose, never self-approve.**
Every write `review.py` makes as `operator` still needs a terminal and has no `--yes`.

## What the policy is

`config/review-policy.json`, schema `review-policy/1`:

```json
{"schema": "review-policy/1",
 "rules": {"registry.exact-duplicate": {"version": 1, "on": true}, "...": {}},
 "remind": {"count": 20, "age_days": 14},
 "enabled": {"hash": "sha256:...", "at": "2026-10-24T10:00:00Z", "by": "operator at a terminal", "user": "login"},
 "history": [{"event": "enabled", "hash": "sha256:...", "at": "..."}]}
```

| # | Rule | Why |
|---|---|---|
| 1 | **The rules are a closed vocabulary.** A rule id the engine does not ship makes the file `invalid`, and an invalid policy decides nothing. There is no rule that accepts a `description`, an `organization` or `local_folders`, and no parameter that widens a rule. | A file an agent could edit must not be able to grant more than the engine already chose to offer. |
| 2 | **It does nothing until a person enables it**, with `full review policy enable` at a terminal, which records the rules' hash, the time and the login. With no file, or `enabled: null`, the policy is `off`: the tick only counts what it would do. | Enabling is the one act by which a person delegates. It is the same act as every other operator write: at a terminal, no `--yes`. |
| 3 | **Changing the rules un-enables them.** The hash covers every rule id, its version and whether it is on. A file whose rules no longer hash to the recorded one is `changed` and decides nothing until it is enabled again. A rule whose meaning changes in a later release gets a new `version`, which changes the hash. | A person enabled exactly what `policy show` displayed, not whatever the file says later. |
| 4 | **A policy decision is never the operator's.** It is recorded as `decided_by = policy:<rule-id>@<hash12>`, with the rule's reason and evidence, and journaled. `owner = operator` and `decided_by = operator` are written only by a person's act. | Rule 3 of the project, and the ledger's own guard (`_check_owner`): an automated writer may not mint operator authority. |
| 5 | **Every decision is reversible.** `full review undo-policy ID` or `--since TIME` returns a decided proposal to `proposed` and removes from the curation file exactly what that decision added, and nothing a later decision or a person changed. | A delegated decision that cannot be taken back is not delegated; it is surrendered. |
| 6 | **A run is bounded.** At most 500 decisions per run, inside a tick step with a 120 s limit that is not one of the steps that must finish (`tick_budget.MUST_FINISH`). What is left is decided on the next tick. | The tick's tail is measured and shared (OBS-220); a queue of a thousand rows must not take the findings' time. |

### The default rules

| Rule | Decides | Why it is safe | Leaves for a person |
|---|---|---|---|
| `registry.exact-duplicate` | a pending proposal with the same target and the same patch as an older pending one becomes `superseded`, naming the older | the older one stays pending with the same content; nothing is lost or applied | the older proposal itself |
| `registry.resources-append` | a `resources`-only patch for a `project:` in the registry, from an `agent:` or `service:` owner, every resource with a kind and an identifier (`proposals.refusal` is empty): `accepted`, appending to `config/project_overrides.json` only the resources not already recorded | resources are append-only (`organizations.merge_resources`): accepting one never replaces what is recorded. A resource already recorded with the same fields is skipped (the measured queue held about nine such duplicates) | a resource whose kind and identifier are recorded with OTHER fields (accepting it would change a record), a target not in the registry, an owner that is not an agent identity, a resource whose `added_by` names anyone but the proposal's owner, a field with a credential shape |
| `registry.aliases-clean` | an `aliases`-only patch for a target in the registry: `accepted` into `config/identifiers.json` | the same two checks the resolver applies: a name already curated for another target (`identifiers.merge_accepted`) and a name the registry has measured on another project (`identifiers.derived_names`) are conflicts, and a conflict is never accepted | every conflict, an unreadable curated table, a field with a credential shape |
| `ledger.fold-same-day` | the superseded readings of one (project, kind, owner, day) group, as `review.py reject-group` computes them (`estate.fold_groups`), are folded: taken out of the person's count, left `proposed` for retention | it writes nothing to the ledger (see below); `undo-policy` brings them back | the latest reading of each group |

A patch that touches anything else (`description`, `organization`, `local_folders`, or a mix of
fields) is left for a person, with the reason.

**Every string of a patch is checked for a credential shape** before any rule reads it
(`review_policy._credential_problem`, over `credential_shape.find`, the shape check agent memory and
the wire's refusals use, UUIDs included). A hit is left for a person with the field's path and the
kind of shape, never the text: a policy writes into curation files that are committed, and a key
there costs a rotation. Evidence is not checked this way (a full commit id is a 40-character hex run);
it reaches the curation file only as the provenance of a row the policy creates.

**`added_by` is the proposer's or nobody's.** A resource's `added_by` is free text from the patch.
A policy writes what the proposal says, so a resource whose `added_by` differs from the proposal's
owner is left for a person: the alternative, overwriting it, would still have the policy choose a
name for a record nobody read. A policy-accepted resource therefore names no one but the agent that
proposed it, and never the operator.

### Why the ledger fold writes nothing to the ledger

`proposed` goes only to `observed` or `rejected` (`store/ledger.py`), `rejected` is terminal, and a
writer other than the operator may only change its OWN record (`_check_owner`). So a policy that
rejected a superseded reading would either mint operator authority, impersonate the record's author,
or make a decision no undo could reverse. The fold is therefore a decision in the policy's journal:
the reading leaves the person's count, retention erases it on its normal schedule, and
`review.py batch prepare` still lists it with the recommendation `reject`, so the one launch turns it
into an operator's rejection. A reading corrected after the fold (a new revision) is no longer
folded.

### What is not a person's

| Rows | Why they do not count as waiting for a person |
|---|---|
| routine conclusions (resting only on commits, dirty and clean files: `estate.conclusion_class`) | retention retires them; `review.py digest` still counts them |
| proposed lessons | a second project's commit corroborates them (`tools/corroborate.py`), 365 days |

Structural and unclassified conclusions count as waiting for a person. Before OBS-227 the Health
page said of all agents' records that nothing needs the person (OSS-50); that sentence still stands
for routine records and lessons, and whether it should change for structural ones is open (OBS-230
in `docs/backlog.md`).

## Applied without a coding agent: the tick step

`tools/tick.py` runs `review.py policy run` as the step `review-policy`, after `corroborate` and only
when the tick refreshed the registry (the alias rule reads the registry's measured names). The run:

1. takes `store/review-policy/lock` (a run that finds it held skips and says so);
2. evaluates both queues against the live curation files and registry;
3. with the policy `on`, first settles any intent an earlier run journaled and never closed (below),
   then applies at most 500 decisions as one intent: the journal lines first (each decision with what
   it will add and its `intent` id), then the curation files (atomically), then the proposals'
   statuses in one transaction, then a line that closes the intent as `done`;
4. always writes its receipt `store/raw/review-policy.json`: the policy's state and hash, what it
   decided, what it would decide if it were on, what waits for a person, and `recovered`, the
   decisions of an unfinished run it reverted.

**A crash between the steps.** A decision is in force only once its intent is closed `done`
(`active_decisions`); until then it is pending (`pending_decisions`). Every review writer (the
tick's run, `undo-policy`, `batch apply`, `accept-proposal` and `reject-proposal`, each under the
lock) calls `recover` first: every pending decision is reverted, never completed, because whether its
file write happened is not recorded. Decisions are reverted in the reverse of the order they were
applied (the journal's line order; each decision also carries `seq`), so the decision that created a
project row is taken back after the ones that appended to it, and no row is left behind with only its
provenance. The revert removes a resource or alias only when it is exactly what the decision would have
added, and a status only when it is the policy's own. A proposal decided since by anyone else keeps
that decision: when it was a rejection, what the run wrote for it is removed all the same; when it was
an acceptance, it is kept as that acceptance's. Each decision's outcome is journaled (`recovered`),
and the intent closes `reverted` when every decision was reverted, `settled` when one was left as a
person decided it. A curation file that cannot be read (broken by hand after the crash) keeps the
intent open: the run names it in `degraded`, applies nothing that tick, and tries again on the next,
never raising. The receipt's `recovery` counts what was reverted, left and kept open. Before this order
(the journal written after the files), a crash after the file write left resources no journal line
named, which no undo could take back. A dry run (`policy run --dry-run`) never recovers; it names a
pending intent in `degraded`.

**A reason quotes no credential.** A reason left for a person can quote the patch (a refusal names
an unknown field, the credential check names a field's path); every such reason, the kind column and
the batch file's patch summary pass through `credential_shape.redact`.

## The remainder: one launch

`full review batch prepare [--out FILE] [--include-routine]` writes `review-batch/1`
(default `store/review/batch.json`): every item waiting for a person, plus the folded readings, each
with `recommended` and `decision` (`accept`, `reject` or `keep`), a one-line `reason`, the
`evidence` it rests on, the `basis` that produced it (`heuristic:<name>` or `rule:<id>`), and a
`hash` of the item as it stood. The heuristics are deterministic:

| Item | Recommendation |
|---|---|
| a proposal `proposals.refusal` refuses, or whose target is not in the registry | `reject`, with the refusal |
| an exact duplicate of an older pending proposal | `reject` |
| a proposal a default rule would accept (the policy off) | `accept`, naming the rule |
| an alias that conflicts | `reject`: accepting it would fail |
| a `description` for a project that has none | `accept` |
| anything else in the registry queue | `keep`, saying what it would change |
| a folded reading | `reject`, naming the reading kept |
| a structural or unclassified conclusion | `keep`: the engine cannot judge it |

A coding agent may read the items and edit `decision` and `reason`. Then a person runs
`full review batch apply FILE` at a terminal. Nothing in the file is trusted as the engine's view,
`recommended` included, since every field of it can be edited: the engine prepares the batch again
from the queue as it stands, and before the one question it shows the totals, a sample and, in full,
every item whose `decision` differs from the engine's recommendation, every `reason` that is not the
engine's, and every item that will not be applied: one the engine did not prepare for this queue
(an id the policy decides on its own, or one that is not in the queue), and one that is no longer
pending. It asks once, then, under the review lock, prepares the batch again and applies every
`accept` and `reject` as the operator (`accept` on a conclusion promotes it to `observed`), each with
`why = "batch <file hash>: <reason>"`. An item whose hash changed since preparation, whose engine view
moved after it was shown, or that is not pending is skipped and named, never applied to something
nobody read. The status updates are conditional (`WHERE status = 'proposed'`), and an accept or reject
that changes nothing is reported as refused. A file that lists one item twice is refused whole.

## Never forgotten

- **Finding `review.queue_waiting`** (warning): items wait for a person and either their number
  reaches `remind.count` (20) or the oldest is `remind.age_days` (14) old. It names the counts per
  queue, the policy's state and the commands: `policy show`, `batch prepare`, `batch apply`.
- **`full doctor`** carries `review`: the policy's state and hash, the last run and what waits.
- **Health** shows the row "review queue" with the same, and `full review policy show` as its command.

## Open (OBS-230)

- Whether Health's "Nothing here needs you" (OSS-50) should change for structural conclusions now
  that they count as waiting for a person. Safe default shipped: they count, the sentence is unchanged.
- Decisions per rule over time (how much the policy did this week) are in the journal and not yet on
  Health.
