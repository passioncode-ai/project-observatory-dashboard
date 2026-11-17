# The configuration door (M14, OBS-563, CP-20)

## What

One module, `observatory/engine/configuration_door.py`, previews, applies and undoes every setting the engine reads.
The CLI (`full configure --list | --plan KEY=VALUE... | --apply DIGEST | --undo ID`) and the MCP tool
`observatory_configure` (actions `list`, `plan`, `apply`, `undo`) are its two faces. The existing
`configure SECTION NAME VALUE` keeps its arguments, checks and answers and writes through the door, so every write is
journalled. Modules M8, M9, M11 and M13 write configuration only through it.

## Data

- The catalogue, `catalogue()`: one `Setting` per key with `type` (`bool`, `enum`, `int`, `number`, `path`,
  `models`, and since OBS-612 `text` and `json`), choices or range, default, file and gate. It is built from the engine's own lists
  (`configuration.known_names`, `INTERFACE_SETTINGS`, `LOCAL_SECTIONS`, `BUDGET_SETTINGS`), the shipped
  `defaults/models.json` and `defaults/cleanup.json` (the disk guard's levels, temporary, build and Trash days, the
  clone age; the cleanup's auto tier), so a new switch is settable the day it ships.
- Files: `config/settings.json`, `config/local.json` (the moved keys of OBS-353 keep their mirror),
  `config/models.json`, `config/cleanup.json`; since OBS-612 also the switch files `auto-update` and `analytics`
  and eight files of their own ([decisions-and-settings.md](decisions-and-settings.md)). No file changes shape.
- Defaults on: `integrations.vulnerabilities` and `integrations.github_issues` (`configuration.DEFAULT_ON`, read
  by `configuration.enabled` and doctor), CONTROL-PLANE D2 and D6.
- Plans: `store/raw/configure-plans/<hex>.json`, kept a day. Journal: `store/logs/configure.jsonl`, owner-only, one
  line per apply or undo: `id` (`cfg_` and 16 hex), `kind`, `at`, `actor`, `digest` or `undoes`, the changes with the
  values before and after (and as stored), and the operator's words when a change needed them.

## Contract

- `plan(changes) -> {digest, changes, before, after, effects, needsPerson, degraded}`: writes no configuration. The
  digest covers the changes and the hash of every file they touch.
- `apply(digest, actor, decision?)`: under the workspace lock, refused `plan_stale` when a touched file changed since
  the plan; writes every change, restoring the files on a failure; journals; the plan is spent (`plan_unknown` after).
- `undo(journal_id, actor, decision?)`: refused `undo_refused` (with the keys) when a file no longer holds what the
  apply wrote, `already_undone`, `journal_unknown`.
- Gate per key: `safe` (reversible, the engine's own behaviour) or the person's (`on`, `set`, `raise`, `lower`,
  `always`, `values`): an integration that sends data out, a feature that deletes or spends, a cleanup that removes
  sooner, a budget raised, the model chain, the secret sources and the backups root. `person:cli` passes it; an agent
  passes it with the operator's words in DEC-0121's shape (`configure.apply` / `configure.undo`, target the digest or
  the journal id), checked by `node/actions.py`.

## Failure behaviour

Refusals are typed and never echo a refused value: `unknown_key`, `invalid_value`, `secret_refused` (any value shaped
like a credential; a path may hold a system's random temporary folder name), `no_changes`, `file_unreadable`,
`plan_stale`, `plan_unknown`, `operator-decision-required`, `decision-invalid` (the contract's spelling since OBS-611,
[decisions-and-settings.md](decisions-and-settings.md)), `journal_unknown`, `already_undone`, `undo_refused`. A file that cannot be read is named in `degraded` and its keys read `null`, never the default. A journal
line that cannot be written is named in `degraded` (`journal-unwritten`); the change stands.

## Tests

`observatory/engine/tests/test_configure.py` (suite `configure`): the catalogue with types, defaults, files, gates and
values; an unreadable file degraded; plan, apply and undo round trip across three files; a stale plan; undo refused
after a later write; back to the default, local keys included; unknown keys and invalid values; secret-shaped keys and
values never echoed or kept; the person's gate for an agent; the old `configure` answers unchanged and journalled; the
CLI flags. The MCP surface is pinned by `test_mcp_wire` (`GOLDEN_TOOLS`), `test_wire_contract` and
`tools/shipped.lock.json`; `configure` is `covered` in [`parity.json`](parity.json).
