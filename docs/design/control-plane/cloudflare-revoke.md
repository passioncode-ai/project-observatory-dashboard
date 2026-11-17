# Cloudflare: revoke a vault-slot token (OBS-575)

## What

`tools/cloudflare.py revoke --vault PROJECT/ENV/NAME` revokes a token the door delivered to a vault slot (every zone
and account preset). Until now `revoke` took only a token filed in the plugin folder, and an early revoke of a
setup token went through the door's functions by hand.

## Data and contract

- The target is checked by `parse_vault_target` (the vault's own name rules) before any call; the slot string is the
  one the managed name records (`... PROJECT/ENV/NAME (managed)`, read by `slot_of`).
- Every stashed account is listed with `_all` (every page); each token whose name records the slot is deleted with
  `_request(... DELETE)`, then the account's tokens are listed again and the token id must be absent.
- Only then `remove_vault_slot` calls `vault.remove_value` (journals `remove`) and the door journals `revoke` by slot
  with the account names and the token count. stdout names the slot, the accounts and the outcome.

## Failure behaviour

Exit 1 and the slot kept, nothing journaled: a delete that fails, a token still listed after the delete, a token
list that cannot be read when no token was found elsewhere, or no managed token naming the slot (the refusal names
`vault.py remove` for a slot whose token is already gone). Exit 2: a malformed target, or `revoke` with both or
neither of a label and `--vault`. No value is read from the slot or printed.

## Tests

`observatory/engine/tests/test_credential_doors.py`: `test_cf_revoke_vault_*` and
`test_cf_revoke_command_line_takes_a_label_or_vault_never_both`, with an injected HTTP layer, a fake vault removal
and a fake token value that must appear in no output or journal. Scenario: OSS-132.
