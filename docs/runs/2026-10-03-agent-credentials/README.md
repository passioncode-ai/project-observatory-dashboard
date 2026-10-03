# Agent credentials through Observatory (W3, first part)

**Objective.** The operator's rule, 2026-10-03: when agents are built, every credential they use
is managed through Project Observatory. This run makes the rule something an agent can follow
and a workflow can check (OBS-10). The rule, the runtime contract and the checks are
[design/AGENT-SECRETS.md](../../design/AGENT-SECRETS.md).

## What changed

| Plan item | Change | Where | Proof |
|---|---|---|---|
| S-01 | a checkpoint declares `credentials: [{project, env, name, purpose}]`; a value in any field is refused | `store/workflow.py` `_credentials` | `test_a_value_is_never_a_credential_address` |
| S-02 | the pack, the acceptance (read again) and `checkpoint_latest` report each credential as `vault`, `env-only`, `missing` or `unknown`, with the command to use or store it, and `credentialsMissing` | `credential_states`, `handoff_create`, `handoff_accept`, `checkpoint_latest` | `Credentials` cases |
| S-03 | known values are labelled by their home, `vault:<project>/<env>/<NAME>` or `env:<project>/<NAME>`, so a redaction marker names the slot | `tools/scan_leaks.py` | `test_redaction_names_the_slot_a_value_lives_in`; scrub markers updated in `test_scrub.py` |
| S-06 | `use_secret.py --vault-only`, and `OBSERVATORY_VAULT_ONLY=1` to make it the default for an agent | `tools/use_secret.py` | `test_vault_only_refuses_a_project_env_file` |
| S-07 | `use_secret.py serve` starts a long-running service from the vault by `exec`, records its consumer; `vault.py rotate` names the services to restart | `tools/use_secret.py`, `tools/vault.py` | `test_serve_starts_a_service_from_the_vault_and_rotation_names_it` |
| S-09 | the rule in the `handling-secrets` skill (a new section, new triggers) and the design page | skill, `docs/design/AGENT-SECRETS.md`, `AGENT-MEMORY.md` | — |

The rule is also written outside this repository, each in its own change:
- the organisation's shared rules (§6, on credentials);
- the skill for building agent services (`fabric-agent-adapter` `building-fabric-services` principle 5);
- the operator's own agent instructions.

**Mutation check.** The consumer list in `vault.py rotate` was switched off and the suite was
watched failing.

**Not in this change.**
- The `observatory-log` plugin version is bumped at the release, as the release steps say, so
  installed copies pick up the skill section then.
- S-04, S-05 and S-08 (the findings and the leak scan over agent memory) are OBS-13.

## Checks run

Checked together on the stack's tip merged with `main` (0.14.0 and the header door), `687320f`
(branch `claude/agent-memory-eval`, which carries W2–W5, OBS-13, OBS-07 and OBS-03), 2026-10-03.
They were not checked per branch.

- `python -m unittest discover -s tests` (repository root): 124 tests, OK.
- `python tests/run_portable.py --jobs 4 --timeout 1200` (engine): 210 of 210 suites PASS,
  6206 assertions.
- Two earlier full runs on this stack found five defects, all fixed before this one:
  - the runtime check did not know the stemmer;
  - the indexer failed on an index without search keys;
  - an erasure test assumed the old index timing;
  - `observatory_record` redaction was quadratic: about an hour on a 2 MB statement, now 0.26 s;
  - a fixture used a key name that matched the maintainer's denylist.

  Load averages of 27–41 timed out `write_surface` (the quadratic redaction) and once failed
  `mcp_inventory`, which passes alone.
- Privacy (`tools/check_public_release.py --history --private-denylist`): the lines this stack
  adds name no private identifier once the fixture key and one private repository name were
  neutralised. The gate's remaining findings are in history already on `main`.
- Hosted CI: the pull request's three required rows.

## Next task

W2 (OBS-09): `observatory_workflow_list`, the operator's `full workflow` commands, and kept steps
named in the review queue.
