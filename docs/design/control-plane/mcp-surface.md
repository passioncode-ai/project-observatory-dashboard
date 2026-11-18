# The MCP surface states itself (OBS-613)

Status: built for wave R3 of the [refactor plan](refactor-plan.md), 2026-11-17.

## What

An LLM client decides what to call from two texts the server serves: the `instructions` and each tool's description
(its docstring). The audit of 2026-11-17 found both behind the tools:

- the instructions sat 5 characters under the 1,800 the wire test allows (a host keeps about 2,048), so the next tool
  named on a line would have pushed a rule past the cut;
- the WRITE line missed `observatory_credential_mark`, `_key_sweep_settle`, `_mcp_accept` and `_doctor`, and named
  the workflow, prompt and paid tools only on other lines;
- the configuration door answered `plan_stale`, `invalid_value` and nine more codes with underscores, beside every
  other tool's `invalid-argument` and the contract's `operator-decision-required` (OBS-611 had fixed only the two
  decision codes); `observatory_mcp_accept` and the configure tool's own guards did the same;
- fourteen read tools never said in their docstring that they answer `degraded`, the field the DEGRADED rule tells a
  client to require; one of them, `observatory_assistant_conversation`, did not answer it on success.

## Contract

```
instructions   first line: "`_x` names the tool `observatory_x`."
               WRITE: every tool whose read_only_hint is false, by name, and "nothing else writes"
               at most 1,650 characters (150 of headroom under 1,800); measured 1,608
tools          every @server.tool declares ToolAnnotations(read_only_hint=...)
               read_only_hint true  -> the docstring says what `degraded` names
               read_only_hint false -> named on the WRITE line
refusals       one spelling, the contract's: lower case words joined by hyphens
```

The short form `_x` saves 11 characters a name; the first line defines it, so a client reads `_doctor` as
`observatory_doctor` without guessing. Every rule the text stated before is kept: SPENDS, the proposals and who
promotes them, the operator's `decision` and the job of `_ops`, SECRETS, DEGRADED, the workflow steps (now on the
WRITE line), START, READ and its paging, and the capabilities (named by their prefixes, as `tools/list` lists them).

The 18 tools that write: `record`, `learn`, `lesson`, `propose`, `org_selection`, `configure`, `key_act`, `ops`,
`checkpoint_write`, `handoff_create`, `handoff_accept`, `secret_request`, `credential_mark`, `key_sweep_settle`,
`mcp_accept`, `doctor` (run, undo), `issues` (its `dismissBanner`), `assistant_ask`. The other 26 read. The new
annotations are hints in `tools/list` (MCP `readOnlyHint`, `destructiveHint`, `idempotentHint`); no tool name, input
schema or answer field changes.

## Refusal codes

`configuration_door.refusal_code(code)` is the one reading: the decision codes as `node/actions.refusal_code` reads
them (OBS-611), then the door's own earlier spelling (`LEGACY_CODES`: `unknown_key`, `invalid_value`,
`secret_refused`, `no_changes`, `file_unreadable`, `plan_stale`, `plan_unknown`, `journal_unknown`, `already_undone`,
`undo_refused`, `actor_refused`, `invalid_argument`, `workspace_refused`) to the hyphenated one. `Refusal(code)` runs
it, so a code handed in the earlier spelling is read as the same and only the contract's is answered, by the CLI's
`configure` and `observatory_configure` alike. The phrase-shaped errors of the oldest tools (`owner refused`,
`unknown project`, `LedgerError`) are not identifier-shaped codes and are pinned by published output schemas
(`record-output.schema.json`); they are left as they are.

## Failure behaviour

Nothing here can fail at run time: the instructions are a constant and the annotations are declarations. A
conversation read whole answers `degraded: []`; the stored file is not rewritten.

## Tests

- `observatory/engine/tests/test_wire_contract.py` `test_the_surface_states_itself`: every tool declares
  `read_only_hint`; the WRITE line names every writer and no reader; every reader's docstring names `degraded`; the
  headroom; no identifier-shaped refusal code with an underscore in `mcp/server.py` or `configuration_door.py`, and no
  docstring naming one.
- `observatory/engine/tests/test_mcp_wire.py`: the served text keeps the headroom; the READ line's paging reads the
  short form.
- `observatory/engine/tests/test_configure.py`
  `test_every_refusal_is_answered_in_the_contracts_spelling_and_the_earlier_one_is_read`.
- `observatory/engine/tests/test_interop.py` `test_a_conversation_read_whole_answers_an_empty_degraded`.
