# M5: the key sweep over the estate's repositories (OBS-554)

Status: built, 2026-11-16. Requirement CP-08, decision D3 and module M5 of [CONTROL-PLANE.md](../CONTROL-PLANE.md).
Public sources: [research.md](research.md), "Key scanners".

## What

| Piece | File | Runs |
|---|---|---|
| The sweep | `observatory/engine/tools/key_sweep.py` | every maintenance pass (`maintenance.step_key_sweep`, in a process of its own), `full key-sweep [--full]` |
| Findings | `observatory/engine/tools/key_sweep_findings.py`, called by `build_findings.py` | every findings build |
| Page section | `observatory/engine/dashboard/key_sweep_page.py` (`BODIES["findings"]`) | every dashboard build |
| Agent read and settle | MCP `observatory_key_sweep`, `observatory_key_sweep_settle` | on request |

Two scanners. **Known value**: the values this machine holds (`scan_leaks.known_values`: vault slots, the env
inventory's secrets, installed OpenRouter keys), compared in memory with every file Git would commit (tracked, and
untracked but not ignored: `git ls-files --cached --others --exclude-standard`) and with every line each commit adds
(`git diff-tree -p -U0 --root`). **External**: gitleaks or Betterleaks when installed (`gitleaks dir` and `gitleaks
git`), always with `--redact`, the report in an owner-only `mkdtemp` folder, `RuleID`, `File`, line, column and
`Commit` copied, the folder removed in a `finally`; the scanner's own output is discarded unread. A `dir` hit on a
file Git ignores is dropped. Provider verification of a found key is never done (it would send the value).

## Data

`store/raw/key-sweep.json` (`key-sweep/1`): `at`, `enabled`, `complete`, `scanners` (`known_value`, `known_values`
count, `external` name or null), `budget` (day, daily and pass seconds, spent, left), `repositories[]` (`repo` the
project's local folder, `project`, `history_through`, `history_complete`, `tree_scanned_on`, `commits_read`,
`sightings_by_rule`, `open`, `degraded`), `sightings[]` and `degraded`.

A sighting is one value, however many places: `fingerprint` (`hmac-sha256:` over `key-sweep/1\0` and the value, keyed
with the workspace's fingerprint salt, `scan_env.salt`), `rules`, `slot` (`vault:<project>/<env>/<NAME>`, `env:...`,
`openrouter/...` or null), `digest_of` (`value`; `match` for an external hit on no known value, the matched span
re-read from the file in memory; `place` when the span could not be re-read), `length`, `places[]` (`where` tree or
history, `repo`, `path`, `line`, `commit`), `slot_state` (current, rotated, gone), `in_tree`, `status`, `closed_by`,
`rotated_at`, `settled`. Never the value, the match, the line or a plain hash.

`key-sweep-state.json` in the store folder: the day, the seconds it has left, and per repository `history_through`,
`history_values` (a digest of the set of values the history was read against), `history_done`, `commits_read`,
`external_through`, `tree_on`, and while a tree is read over several passes `tree_cursor`, `tree_found` (digests and
places) and `tree_unread`. `key-sweep-settled.json`: `{fingerprint: {at, by, why}}`, written only by settle.

## Wireframe (Findings page, above the list)

```
Keys found in repositories
Swept 2026-11-16 10:00 UTC: our own values and key-shaped text ... never a value.
External scanner: not installed; our own values are still looked for.
REPOSITORY  PROJECT            OPEN  BY RULE          HISTORY                  WORKING TREE
alpha-web   project:alpha-web  1     known-value 1    Read to the last commit  2026-11-16
beta-api    project:beta-api   0     -                Read in part             2026-11-16
NOT FULLY READ
- beta-api: 1 file(s) too large or unreadable; not read
```

## Contract

- **Budget.** A pass spends at most 120 s and a UTC day at most 900 s. Trees first (one not finished resumes before
  any other; each is read once a day), then histories, oldest commit first, resuming after the last commit read. A
  history read against a different set of values (a new slot, a rotation) is read again from its start; a history
  rewritten under the last commit read is read from its start and named.
- **Finding** `secret.in_repository`, one per open sighting, subject `secret:<folder>/key-sweep/<digest prefix>`
  (attributed by `survey.finding_matcher`). Critical when a slot still holds the value; warning otherwise (the
  board's vocabulary has no "high"). It names the slot, the places and the rules.
- **Closing.** Gone from every working tree AND the slot holds another value (rotated); for a value no slot holds
  now, gone AND settled (`full key-sweep --settle FINGERPRINT --why TEXT`, `observatory_key_sweep_settle`). Settle is
  refused while the value is in a tree (`still-in-tree`) or its slot holds it (`slot-holds-it`). A value no longer
  known (rotated, or an external hit) is found again in the files it was seen in by its digest and length alone.
- **Switch.** `integrations.key_sweep`, on by default (`configuration.DEFAULT_ON`; listed with the local
  integrations of the configuration door: nothing is sent). Off writes `enabled: false` and the findings go quiet.
- **MCP.** `observatory_key_sweep(repository?)`: per repository what was read and the open sightings by rule, then the
  sightings with places; `degraded` names a sweep that has not run, is off, or read a repository in part;
  `unknown-repository` is a typed refusal.

## Failure behaviour

- No salt: nothing is swept, exit 1 (the maintenance step records `failed`). No registry: the step answers
  `no-registry` without starting a process.
- A file over 2 MiB, unreadable or binary is skipped and counted in `degraded`; a commit adding over 16 MiB is named.
- Out of time: the tree keeps its last whole read until the resumed one finishes; the history resumes next pass.
- The external scanner absent: `scanners.external` is null, not a degradation. A run that fails, times out or
  leaves no readable report is named in the repository's `degraded`, and the report is still deleted.
- No value held: the known-value scan has nothing to look for, named `no-values`.

## Not done here

- A value moved into a file it was never seen in, after its slot was rotated, is found only by the external scanner.
- History places of a commit read before a value existed are found when that history is read again (any change of
  the value set restarts it), not sooner.
- The leak register is not written: a sighting is a finding, and `vault.py leak` stays a confirmed act.

## Tests

`observatory/engine/tests/test_key_sweep.py` (suite `key_sweep`): a planted fake key found in a tree and in history
with its place and fingerprint; one value in two files is one sighting; ignored files skipped, untracked ones read;
the stored document and state grep clean of the value, its parts, its plain hash and its line; the history resumes
after the budget ran out, each commit read once; a spent day reads nothing until tomorrow; a new value rereads the
history; the scanner absent; the report redacted, deleted and its secret fields never kept; a failed scanner named and
its report deleted; a rotated slot closes the finding once the value is gone; an unknown value closes only when gone
and settled; settle refusals; the agent view; the page section in both languages; the maintenance step and its
switch. `test_mcp_wire` calls both tools over stdio before any sweep.
