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
