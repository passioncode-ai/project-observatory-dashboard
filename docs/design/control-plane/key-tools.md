# The key page's acts for an agent (M10 part 2, first wave: OBS-559, CP-18, decision D8)

## What

The local control server (`tools/keyserver.py`) offers the person at the key page eight acts that had no MCP tool:
`mint`, `limit`, `disable`, `enable`, `rotate-key` and `revoke` of an OpenRouter key, `leak` (mark a vault slot's
value seen somewhere) and `annotate` (sign a credential with its purpose and evidence). Two MCP tools give an agent
the same acts with the same data and the same effect:

| tool | actions | key server act |
|---|---|---|
| `observatory_key_act` | `mint`, `limit`, `disable`, `enable`, `rotate`, `revoke` | `act_mint`, `act_limit`, `act_disable`, `act_enable`, `act_rotate_key`, `act_revoke` |
| `observatory_credential_mark` | `leak`, `annotate` | `act_leak`, `act_annotate` |

## Decision: two tools, not eight and not one

- One tool per coherent family with an `action` argument, as `observatory_configure` and `observatory_org_selection`
  do. The six key acts share one object (an OpenRouter key by its ledger name or label) and one door
  (`tools/openrouter.py`); one tool keeps their arguments and refusals in one place.
- `leak` and `annotate` are records about a credential, not acts on a provider key: their object is a vault slot or a
  registry credential id, their key-server effect is `draft` (`ACTION_SCOPES`), and they need no decision. Folding
  them into `observatory_key_act` would give that tool two unrelated argument sets.
- No existing tool fits them: `observatory_credentials` and `observatory_key_consumers` are reads (a write would
  change their read-only annotation), `observatory_key_sweep_settle` settles a sweep sighting by its fingerprint, and
  `observatory_configure` refuses anything shaped like a credential by design.
- `rotate` is the tool's word for the key server's `rotate-key`; the key server keeps `rotate` refused because that is
  the vault's verb (a value on stdin), and this tool never reaches the vault's rotation.

## Same effect

`key_acts.py` calls `keyserver.ACTIONS[name](body)`: the page's own function, with its checks (a destination the door
owns or a known vault slot, a positive ceiling, a key in service not revoked, a rotation held while a copy of the old
value is out of reach), its journal line written before the act, and its door call. Nothing in `key_acts.py` mints,
caps, toggles, revokes, marks or signs; a test reads its source for the door verbs to keep it so. The key server's
exceptions become typed refusals by the same mapping its HTTP route uses.

## The gate

Over HTTP the key server takes an agent's call only with a Fabric delegation token for the act's scope
(`keyserver.ACTION_SCOPES`: `charge` for mint, `delete` for revoke). A stdio MCP server has no caller identity to
check, so an act that spends or cannot be undone takes the operator's words instead, in contract DEC-0121's shape,
checked by `node/actions.py` as `observatory_configure` checks them:

| act | needs `decision` | decision `action` | `target` |
|---|---|---|---|
| mint | always (spends) | `keys.mint` | the destination, normalised (`observatory`, `vault:<project>/<env>/<NAME>`) |
| limit | when the new ceiling is above the one the door's ledger records, or the ledger cannot say | `keys.limit` | the key's name as given |
| rotate | always (mints a successor, revokes the old key) | `keys.rotate` | the key's name |
| revoke | always (cannot be undone) | `keys.revoke` | the key's name |
| disable, enable, leak, annotate | never (reversible, or a draft record) | | |

The decision is refused when it is missing (`operator-decision-required`, naming the action and target to ask for),
not one object, for another act or object, older than 24 hours or carrying a credential shape (`decision-invalid`).
A decided act's journal row in `store/logs/keyserver.jsonl` keeps the decision (`by`, `via`, `action`, `target`,
`instruction`, `at`, `source`, `caller`) beside `caller: agent:mcp` and `caller_verified: false`; the key server's
`_RELAYED` context carries it to `audit()`.

An annotation an agent signs is `signed_by: agent:mcp`, not `operator` (`keyserver._signer`): the page still signs
as the operator, a delegation token signs as the proven caller.

## Contract

```
observatory_key_act {action, name?, destination?, limit?, limitReset?, account?, project?, decision?}
  -> {action, ok: true, ...the door's answer..., recordedAs?: {by, via, caller}}
observatory_credential_mark {action, project?, env?, name?, where?, id?, purpose?, evidence?, owner?, rotationDays?, tags?}
  -> {action, ok: true, marked, receipt}            (leak: the vault's own receipt line)
  -> {action, ok: true, id, signed_on}              (annotate)
refusal -> {error: {code, message, action?, target?}, degraded: []}
  codes: invalid-argument, operator-decision-required, decision-invalid, not-found, held, provider-refused,
         unconfirmed, failed
```

An argument another action takes is refused, never dropped. Writes carry no `degraded` on success (the server's
instructions: writes have none); every refusal carries `degraded: []`.

## Failure behaviour and secrets

- No key value crosses: the door writes a minted or rotated value straight to its destination; nothing here reads
  one. Any string argument shaped like a credential (`credential_shape`) is refused by field name and never echoed;
  refusals are scrubbed with `credential_shape.redact`; the journal scrubs every string it writes.
- A ledger that cannot be read makes a `limit` call a raise (needs the decision), never a silent pass.
- A rotation held for a consumer answers `held` with each holder named; the old key stays alive.
- A leak mark succeeds only on the vault's receipt line, never on its exit code alone (`unconfirmed` otherwise).

## Tests

`observatory/engine/tests/test_key_acts.py` (suite `key_acts`): each act through a stubbed door, a stand-in vault
program and the real signer over a temporary file; the gate (missing, wrong action, wrong target, old, credential in
the words); the ceiling comparison; the journal's caller and decision; arguments of another action; a pasted fake
key refused and absent from every answer, refusal and journal line; the source checks that both tools call
`key_acts` and that `key_acts` calls only `keyserver.ACTIONS`. `test_mcp_wire.py` calls both tools over stdio
(a mint without words, a pasted value in a leak mark). `tests/test_agent_parity.py` holds the parity list.
