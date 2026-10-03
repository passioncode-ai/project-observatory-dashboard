# Credential checks that report (W3, second part)

**Objective.** OBS-13: the rule "every credential an agent uses comes from Observatory" now
reports when it is broken, instead of only being written down. Design and checks:
[design/AGENT-SECRETS.md](../../design/AGENT-SECRETS.md).

| Plan item | Change | Proof |
|---|---|---|
| S-04 | a workflow write or an agent note that replaced a KNOWN secret value is journalled by slot name (`memory-redactions.jsonl`), and `secret.seen_in_agent_memory` (critical) carries the `vault.py leak` command | `test_a_known_value_in_memory_is_journalled_and_reported` |
| S-05 | `scan_leaks` reads the Observatory's own store beside the companion's, incrementally, and reports it under `observatory_store` | `test_the_observatory_store_is_in_the_leak_scan` |
| S-06 | `agent.secret_fallback_used` from use_secret's journal | `test_a_run_that_fell_back_to_a_dotenv_is_reported` |
| S-08 | `agent.secret_outside_vault` from the env inventory and the vault listing | `test_secrets_outside_the_vault_are_named_with_the_command` |
| — | `observatory_record` redacts an agent's note like a checkpoint | the same `memory_redact` |

Agent projects are measured, never guessed: a project a workflow declared a credential for, or
one with a Fabric agent manifest. `test_quiet_when_the_rule_holds` checks that the findings stay
silent when nothing breaks the rule.

## Checks run

CHECKS_PLACEHOLDER

## Next task

W6: OBS-07 (the evaluation set), then OBS-03 (search).
