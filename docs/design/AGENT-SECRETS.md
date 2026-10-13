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
| Issue a project's own key from its spend policy (OBS-58): every `project-key` source becomes a key issued for that project into the slot it names; the keys' monthly ceilings together are at most the project limit (ENG-002) — split equally, rounded down to the cent, unless `--split` gives each slot its amount or `--limit` one ceiling per key; another project's slot is refused as a borrow; a held slot is read at the provider, and a disabled or deleted key there is `held-disabled` or `held-missing` (exit 1), never in place; ceilings that go down move first; a failure halfway exits 1 with `partial` and the steps already done; a second run moves only a changed ceiling. Only `issue-for-policy` upholds "together at most the project limit": the keys page's ceiling action and `openrouter.py limit` can raise one key above its share, and the next `issue-for-policy` run brings the keys back under the limit. `--dry-run` reads the provider for every held slot: with no stashed admin for a slot's account (or a row naming none while several are stashed and no `--account`), the slot reads `held-unread` and the run exits 1. After a ceiling moves, set `keyCeilingsUsd` in `config/meter.json` to the amounts the plan prints. The total holds at every moment: every key under `vault:<project>/` counts, a key the policy no longer names is reported `not-in-policy` and not moved, an unread key counts at its ledger ceiling, raises run only when the total after the run fits (else `blocked`, exit 1), and one run holds a lock beside the ledger at a time. Totals are whole cents. A key minted whose delivery was never recorded keeps a pending ledger row that holds its slot (`held-pending`, nothing minted there again). A run that leaves the total past the limit prints `OVER LIMIT by $X now ($A against the $L limit); it ends after: revoke A, …` (`OVER LIMIT: unbounded now` when a key has no ceiling) and exits 1. **The one exception to the invariant is `rotate`** (and `rotate --leaked`): it delivers the successor before it deletes the predecessor, so while it runs the slot holds two live keys and the project's total can reach the slot's ceiling twice over. Until the J-3 change counts them, a rotate that FAILS (a provider error after the mint, a timeout, a kill, a vault put that fails, a predecessor delete that fails) can leave the successor `NAME-next` live and untracked, or the predecessor live and uncounted (the row then reads `rotation_status: successor-delivered-predecessor-pending` with its `predecessor_hash`); `issue-for-policy` does not see either. The remedy: at the provider, delete whichever of the two keys the slot does not hold, then run `issue-for-policy`. A mint the provider answered with a 5xx or a timeout keeps its pending row, because the key may exist; past the listing, where the name cannot be looked up, `revoke NAME --forget-pending --reason "…"` clears a pending row with no hash once the key is gone; its reason must first be recorded as an attestation, or the row stays. The attestation is not provider verification; completion is a separate event after the ledger write | `openrouter.py issue-for-policy --policy FILE [--limit USD \| --split REF=USD,…] [--dry-run]` | yes |
| Store a key the user has | `vault.py put PROJECT ENV NAME`, value on stdin | yes |
| Find what exists | `observatory_credentials` (MCP) or `use_secret.py names PROJECT`; names only | yes |
| Run with it, from the vault only | `use_secret.py run --vault-only [--env ENV] PROJECT NAME -- COMMAND` | yes (`--vault-only` new) |
| Make vault-only the default for an agent | `OBSERVATORY_VAULT_ONLY=1` in the agent's environment | yes (new) |
| Say what a workflow needs | checkpoint `credentials: [{project, env, name, purpose}]` | yes (new) |
| Know before a handoff that a key is missing | the pack and the acceptance carry each credential's state and `credentialsMissing` | yes (new) |
| Run a long-lived service with its keys | `use_secret.py serve [--env ENV] --consumer LABEL PROJECT NAME[,…] -- COMMAND`: vault only, then `exec` — the service is the process, gets launchd's signals and keeps its own output | yes (new) |
| Release a slot only under a grant that names it | `FABRIC_DELEGATION_TOKEN` in the agent's environment: its token from Fabric's hub (aud `project-observatory`, scope `secret.use`); `run`, `serve` and `header` then release a vault slot only when a grant the token names has `credentialSource {kind: project-key, ref: PROJECT/ENV/NAME}`, never read a `.env`, and remove the token before the child starts (`docs/design/METER.md` M5) | yes (new; checked when a token is present; never demanded on the operator's machines, METER.md M0) |
| Put a DigitalOcean database's connection URL in a slot, and rotate its password with every named consumer | `digitalocean.py db credential CLUSTER USER --db NAME --to vault:PROJECT/ENV/NAME`; `digitalocean.py db rotate … --app ID --spec-env ID:KEY` (doctl's current context is the credential; `db consumers CLUSTER` lists the App Platform apps that bind the cluster) | yes (new) |
| Give an MCP server its key as a header, never in the client's config | `vault.py bind PROJECT ENV NAME --header-for URL` (https, or `http://127.0.0.1:<port>` / `http://[::1]:<port>` for a loopback server; `localhost` refused), then `use_secret.py header PROJECT NAME` as the client's helper: Claude Code's `headersHelper`, or Codex's `http_headers_helper` with `--server-url URL`, since Codex exports no server URL; two URLs that disagree are refused (`url-conflict`) and the audit row names the caller | yes (loopback and `--server-url` new) |
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
| a slot is released only under a grant naming it; another slot is `no-grant`, a capability source `credential-source-refused`, a revoked grant `grant-revoked`, another scope `principal-unproven`; the child does not inherit the token; `always` refuses without one; the journal names the verdict, never the token | `test_a_slot_is_released_only_under_a_grant` |
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

## DigitalOcean rotation evidence

DigitalOcean rotation verifies every selected `--spec-env APP:KEY` literal before any reset.
Its name is not evidence: a plain value must equal the selected database user's current
provider password, or be a database URL with that password, user, engine and the provider's
connection host/port. Opaque encrypted values and unverified pool endpoints are refused;
migrate those consumers to App Platform database bindings before rotation. No command in
this flow prints the current password. Only the named consumers are moved, with downtime
between reset and ACTIVE; ACTIVE is not application health verification.

The door writes an owner-only receipt under `store/digitalocean-rotations/` before reset,
initially `reset: unknown`. A lost or malformed response, timeout or nonzero CLI result can
follow a committed reset: exit 5 means **unknown**, not “nothing changed”. Never repeat
`db rotate` to recover it. Use `db credential` to deliver the current value into the selected
vault slot, then reconcile each consumer without another reset. Exit 1 is a refusal before
reset; after an attempt, 2 means deployment unconfirmed, 3 vault delivery failed, 4 an app
update/redeploy or temporary spec cleanup failed, and 6 receipt update failed. The largest
code wins. Independent consumers continue after a confirmed reset despite another consumer's
failure. Provider/vault error bodies are withheld. A crash or failed final receipt write
leaves the earlier uncertain receipt; there is no automatic reconciliation, retry or claim
of a transaction across provider and vault. A failed temporary-file cleanup may leave its
owner-only spec file and is reported for local recovery.

Reset, app updates and redeploys each make one attempt; only read calls retry.
A lost/nonzero mutation response leaves that consumer `unknown`, even if the provider
already applied it. The receipt distinguishes this from `not_started` for a local
pre-call failure. Read current spec and deployment state before any retry; a blind
redeploy is not recovery for a literal. Independent peers still continue. Deployment
confirmation accepts a validated phase equal to `ACTIVE`, never a suffix or arbitrary
provider text; malformed metadata remains unconfirmed and its body is not printed.


Verified by `tests/test_digitalocean_safety.py` and `tests/test_digitalocean_door.py`
using fake provider responses and scratch vaults. Live provider acceptance is separate.
