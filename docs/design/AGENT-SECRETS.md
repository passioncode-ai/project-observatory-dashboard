# Agent secrets: every credential an agent uses comes from Observatory

**The rule.** Every credential an agent uses — a one-shot agent, a long-running agent service, or
code an agent writes for one — is issued, stored and named in Project Observatory, discovered by
name, and injected at run time. An agent has no `.env` of its own, no key file beside its data,
and never a value in code, configuration, a commit, a prompt, a log or agent memory.

Why one home: a key with one home can be rotated, its exposure recorded and settled, its
consumers listed, and its leak found by an exact-value scan. A key copied into an agent's own file
drifts from the vault on the first rotation, is invisible to the leak scan's list of known values
until someone inventories it, and survives the agent that needed it.

## How an agent follows it today

| Step | Command or tool | Shipped |
|---|---|---|
| Issue a key from a provider straight into a slot | `cloudflare.py issue --vault PROJECT/ENV/NAME`, `openrouter.py issue --to vault:PROJECT/ENV/NAME` | yes |
| Store a key the user has | `vault.py put PROJECT ENV NAME`, value on stdin | yes |
| Find what exists | `observatory_credentials` (MCP) or `use_secret.py names PROJECT`; names only | yes |
| Run with it, from the vault only | `use_secret.py run --vault-only [--env ENV] PROJECT NAME -- COMMAND` | yes (`--vault-only` new) |
| Make vault-only the default for an agent | `OBSERVATORY_VAULT_ONLY=1` in the agent's environment | yes (new) |
| Say what a workflow needs | checkpoint `credentials: [{project, env, name, purpose}]` | yes (new) |
| Know before a handoff that a key is missing | the pack and the acceptance carry each credential's state and `credentialsMissing` | yes (new) |
| Run a long-lived service with its keys | `use_secret.py serve [--env ENV] --consumer LABEL PROJECT NAME[,…] -- COMMAND`: vault only, then `exec` — the service is the process, gets launchd's signals and keeps its own output | yes (new) |
| Be told which services to restart after a rotation | `vault.py rotate` lists each recorded consumer with its `launchctl kickstart -k` command | yes (new) |

## Credentials in agent memory

A workflow declares the credentials it needs, by name:

```json
"credentials": [{"project": "alpha-web", "env": "prod", "name": "STRIPE_KEY", "purpose": "charge cards"}]
```

- Each field is an address. A value in any of them is refused, not redacted, because this field
  holds addresses only (`store/workflow.py`, `_credentials`).
- `observatory_checkpoint_latest`, the handoff pack and the acceptance each report every
  declared credential as one of:

  | State | Meaning |
  |---|---|
  | `vault` | the slot exists for that environment; the row carries the `--vault-only` command that uses it |
  | `env-only` | the project holds the name in its own `.env` and not in the vault. It works, and it breaks the rule; the row carries the `vault.py put` command that moves it |
  | `missing` | neither; the row carries the `put` command |
  | `unknown` | the vault directory could not be listed. It is said rather than guessed |

- `credentialsMissing` is true when any credential is `missing` or `env-only`. The next executor
  stops before the step that would fail, instead of failing half-way through it.
- The acceptance reads the vault again rather than trusting the pack. A key put into the vault
  after the pack was made counts.
- A value written into memory by mistake is replaced before it is stored. When it is one of the
  workspace's known values, the marker names its home: `[redacted:vault:PROJECT/ENV/NAME]` for a
  slot, `[redacted:env:PROJECT/NAME]` for an env file. Record that as an exposure with
  `vault.py leak`.

## Checks

| Check | Status |
|---|---|
| `use_secret.py --vault-only` refuses a name held only in a `.env`, and says how to store it | `test_vault_only_refuses_a_project_env_file` |
| a credential address carrying a value is refused | `test_a_value_is_never_a_credential_address` |
| a handoff reports `vault`, `env-only`, `missing`; acceptance re-reads the vault; an unreadable vault is `unknown` | `Credentials` cases in `tests/test_workflow_memory.py` |
| a redaction marker names the slot | `test_redaction_names_the_slot_a_value_lives_in` |
| `serve` starts the service from the vault as the same process, refuses a `.env`, and the rotation names it | `test_serve_starts_a_service_from_the_vault_and_rotation_names_it` |
| a finding when an agent's project keeps a secret in its `.env` that the vault does not hold (`agent.secret_outside_vault`) | `test_secrets_outside_the_vault_are_named_with_the_command` |
| a finding when a run for an agent's project took a key from a `.env` (`agent.secret_fallback_used`) | `test_a_run_that_fell_back_to_a_dotenv_is_reported` |
| a finding when a known value was written into agent memory (`secret.seen_in_agent_memory`, critical); the write is stored redacted and journalled by slot name | `test_a_known_value_in_memory_is_journalled_and_reported` |
| the Observatory's own store is in the leak scan: a sighting there means the redactor missed one | `test_the_observatory_store_is_in_the_leak_scan` |
| an agent's note (`observatory_record`) is redacted like a checkpoint | `memory_redact` on the record tool |

## Which projects are agents'

The two findings about a project's keys judge only projects measured as an agent's: a project
a workflow declared a credential for, or one whose local folder carries a Fabric agent manifest
(`fabric-agent.json`). A project an agent merely touched is not judged; guessing would make
every repository an agent's (`tools/agent_secret_findings.py`, `agent_projects`).

## Edge cases

- **Remote services** (a Worker, a Heroku app) cannot read the local vault. The key is issued and
  recorded here, pushed to the platform, and the movement is recorded with
  `vault.py moved --at PROVIDER`. The platform's copy is then known rather than invisible.
- **Rotation and running services.** A value is read when a service starts, so a running
  service keeps the old one until it restarts. `serve` records each consumer by its label and
  `vault.py rotate` names them with the restart command. A service started some other way is
  not on that list.
  `shared_with` in `observatory_credentials` names the other projects holding the same value.
- **The known-value cache** in the redactor reloads when the vault or the env inventory changes.
  A value that has no credential shape and has never been stored anywhere cannot be recognised:
  redaction is a safety net, not a licence to paste.
- **Public code.** Agents published as open source name keys by placeholder (`EXAMPLE_API_KEY`)
  and read them from their environment. Observatory is how the operator's machine fills that
  environment, not a dependency the code must import.
