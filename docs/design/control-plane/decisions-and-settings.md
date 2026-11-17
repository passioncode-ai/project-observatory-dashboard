# One decision check, one settings door (OBS-611, OBS-612)

Status: built for wave R3 of the [refactor plan](refactor-plan.md), 2026-11-17.

## One operator-decision check (OBS-611)

### What

Seven doors take the operator's relayed words (contract DEC-0121 AP-2) before they act: the configuration door
(`configuration_door.py`), the key acts and the operations verbs (`key_acts.py`, whose `decided` `ops_acts.py` reuses),
the organization selection (`org_selection.py`), the secret prompt's two agent twins (`secret_prompt.py`) and the
own-share acts (`own_share.py`). Each wrapped the contract's `actionDecision` (`node/actions.action_decision`) its own
way. They now call one function, `node/actions.decided`, and keep only their own wording of the refusal.

### Contract

```
decided(action, target, caller, now, decision=None)
  -> {ok: true, record, recordAs}          the operator's words; record as a journal keeps it, recordAs the node's
  -> {ok: true, person: true, record}      a person's caller without words
  -> {ok: false, code, why[, fault]}       why: absent | shape | unicode | fault | contract | target | credential
```

The rule, in order:

1. no decision (absent or JSON null) from `person:cli` or `person:page` is the person's own act;
2. no decision from anyone else is `operator-decision-required`;
3. a decision that is not one JSON object, or whose text is not valid Unicode, is `decision-invalid`;
4. `action_decision` (T0) checks it; its refusal is answered, a fault of the node is `unavailable` with the fault;
5. the words name this object as `target`, except for an act whose object is in the words (`OBJECT_IN_WORDS`:
   `share.open`, whose link pins the sender's key);
6. nothing kept verbatim (the words, the target, where they were said) is shaped like a credential, a `sha256:`
   digest and the act's own object as a whole token aside (same exception).

One vocabulary: the contract's `operator-decision-required` and `decision-invalid`. The configuration door's earlier
`operator_decision_required` and `decision_invalid` are read by `refusal_code` and answered in the contract's spelling
(`docs/COMPATIBILITY.md`, "One operator-decision check"). Each door words its refusal from `REASONS[why]`, except
`absent`, whose words name the door's own act and object.

### What changed, door by door

`tests/test_decision_check.py` runs every door over the same ten inputs (absent, null, not an object, valid, no
target, another target, a credential in the words, a digest in the words, the object in the words, a lone surrogate)
and pins the answer. Its `MATRIX` keeps each changed answer with the answer before and the reason:

| Door | Changed | Why |
|---|---|---|
| configuration | the two codes' spelling | the contract's, as every other door |
| configuration | a lone surrogate was accepted | the journal could not write it |
| configuration | words a person's caller carries were ignored | words are read, never ignored |
| key acts | a `sha256:` digest in the words was refused | a digest is not a credential |
| key acts | a lone surrogate was accepted | the journal could not write it |
| secret prompt | null was `decision-invalid` | null is no decision, as at every door |
| secret prompt | a decline without `target` was accepted | words name the request they decline |
| secret prompt | credential-shaped words were kept | the audit record keeps the words |
| own share | a decision without `target` was accepted | OM-1.3 names the share id as target |
| own share | credential-shaped or non-Unicode words were kept | the journal keeps the words |

The organization selection's answers did not change (it already held the strictest rule but one: its caller `cli` is
now `person:cli` inside the check).

### Failure behaviour

A node fault (an unreadable clock, no caller) is never read as a decision: `unavailable`, naming the fault. Every
refusal keeps the door's own exception type and answer shape; no answer echoes the words.

### Tests

`observatory/engine/tests/test_decision_check.py` (suite `decision_check`); the doors' own suites (`configure`,
`key_acts`, `ops_acts`, `org_selection`, `secret_prompt`, `share_cli`, `node_actions`).

## Every setting through the one door (OBS-612)

### What

The configuration door (`configuration_door.py`, OBS-563) declared the keys of four files. Ten more held settings
with their own readers and writers. Each is now a set of declared keys with a type, a default, its file and its gate;
`list`, `plan`, `apply` and `undo` (the CLI's `configure` and `observatory_configure`) reach them like any other.

| File | Keys | Gate (person) |
|---|---|---|
| `auto-update` (home, one word) | `updates.auto` | always: both directions are the person's |
| `analytics` (home, one word) | `analytics.send` | on: turning it on sends data out |
| `config/meter.json` | `meter.issuer`, `hubJson`, `jwks`, `grantsDir` | set: whose tokens the meter believes |
| | `meter.estateMonthUsd` | raise |
| | `meter.alertRatio`, `reservationSeconds`, `keyserverRequiresDelegation` | never |
| | `meter.slotsRequireGrant` | the value `off` |
| | `meter.keyCeilingsUsd` (an object) | always |
| `config/retention.json` | every day count and keep count, `retention.ledger.*` days | lower: removes sooner |
| | `retention.ledger.never`, `owner_exempt`, `kinds` | always |
| `config/machine.json` | the watch's thresholds, `machine.locations` | never: nothing is removed |
| `config/review-policy.json` | `review.rules.<rule>` | on |
| | `review.remind.count`, `age_days` | never |
| `config/machine-backup.json` | `machine_backup.enabled` | off |
| | `transcripts` | on |
| | `include`, `roots` | set |
| | `exclude`, `limits.*` | never |
| `config/relay.json` | `relay.enabled`, `trusted_proxy` | on |
| | `relay.origin`, `host` | set |
| | `relay.port` | never |
| `config/tracked.json` | `tracked.local_folders`, `dismissed_local_folders` | never |
| `config/organizations.json` | `organizations.organizations`, `projects` | always |

Two kinds join the door's types: `text` (one line, at most 500 characters) and `json` (a list or an object, at most
64 KB and 16 levels deep), and one gate rule, `off` (turning a switch off needs the person).

### Data: the same files, the same formats

No file moves or changes shape, so an existing install reads the same values:

- A switch file is written by its owner's writer (`maintenance.write_switch`, `usage_analytics.write_switch`), so
  analytics off still deletes the install id and the queue; back to the default removes the file (on). The legacy
  `updates.auto: false` in settings.json still reads as off while the file is absent.
- A JSON file the door creates starts from its reader's whole document (the shipped `retention.json`, `machine.json`
  and `organizations.json`, `review_policy.default_document()`, the relay's schema, `tracked.empty()`), so the reader
  never meets a partial file.
- retention.json and machine.json are read key by key without defaults (`store/retention.py`), and the review policy
  refuses a rule without its `on`: in these files and organizations.json, back to the default writes the default
  rather than removing the key.
- Before anything is kept, the file as the plan would leave it is checked by its owner (`tracked.validate`,
  `review_policy.parse`, the relay's schema): what its reader would refuse is `invalid_value`.

### Readers through the door

`configuration_door.read(key)` answers one value from its own file only (None when the file cannot be read), and
`switch_word(file)` is the one reading of a switch file. Routed through them: `maintenance.read_switch` and
`auto_enabled` (`updates.auto`), `usage_analytics.read_switch` (`analytics.send`), the events collector's window
(`retention.events_days`; a missing retention.json now reads the default 365 days, not "no window") and the estate
spend finding (`meter.estateMonthUsd`, `meter.alertRatio`). The readers of a whole document (the meter, the store's
retention, the machine watch, the review policy, the machine backup, the relay, the tracked folders, the
organizations) keep reading their file, which the door writes in their format; the board row lists them.

### Never a credential

No declared key holds a secret's value. Every text the door would keep, inside a structure too, is refused
`secret_refused` when shaped like a credential (a path's random temporary folder name aside), and never echoed.

### Failure behaviour

A file that cannot be read is named in `degraded` and its keys read `null`, never a default; a plan that touches it
is `file_unreadable`. A switch file that is a link is no switch, and the door writes none through one.

### Tests

`observatory/engine/tests/test_configure.py`, class `EverySettingThroughTheDoor` (suite `configure`): each file's
keys with their type, default, file and gate; the auto-update switch through plan, apply and undo, the legacy setting,
and one reading of both switch words; analytics off deleting the install id; retention keeping every key and its
reader reading through the door; a file the door creates read whole by its owner; a structured value its owner
refuses; the meter's limit and its gate.
