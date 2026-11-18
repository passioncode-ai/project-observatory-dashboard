# Keys by organization (OBS-604, CP-10)

Status: built 2026-11-18 on `refactor/r4-org-keys`, wave R4 of the [refactor plan](refactor-plan.md). It finishes
the halves [keys-by-source.md](keys-by-source.md) (OBS-556) left: what `shared` means, whose each key is, and who
reads it.

## What

Every vault slot keeps one class, the first that holds:

| class | measured from | `sourceContext` |
|---|---|---|
| `issued` | a door's ledger or record, or a credential request's `provenance.issuer` | door, account, record |
| `received` | `vault.py put --from LABEL` | `from`, `at` |
| `shared` | handed to another party: a peer share this node sent (`node/shares/sent/<id>/share.json`), a credential request delivered into an organization's context (`provenance.recipient.context` is `org:ID`), or a key item decided `hand-over-revoke` or `hand-over-keep` in an organization's applied selection (`org_selection.current`) | `to` (each party) |
| `copied` | the same keyed digest under another folder of this vault (`vault_digests`); this was `shared` in 0.40.0 to 0.42.x | `with`, `measuredAt` |
| `unknown` | none of the above | `why` |

Beside the class, whatever it is:

- `sharedTo`: each outward record, `{kind: peer-share | credential-request | org-hand-over, to, record, ...}`. A peer
  share also says its `state` and `takenBack` (a revocation was recorded); a hand-over its `decision`.
- `copiedWith`, and `sharedWith` under its published name: the other slots holding the same value.
- `context`: whose the key is, from the project its vault folder belongs to, read the way `full org keys` reads a
  project (`org_view._vault_resolver`, `organizations.reading`): `personal` (the default organization, a folder no
  project holds, or no `organizations.json`), `organization` (with `organization` and `label`), `not-ours`, or
  `undecided` (a conflict, or the registry's projects not read).
- `consumers` (in `observatory_credentials`): who reads the value, from every source the revoke guard reads
  (`key_consumers.GUARD_SOURCES`): the OpenRouter scan, asked by the OpenRouter ledger's hash, label and name for a key
  that door delivered; Heroku config vars and inventoried env files, compared with the slot's value in memory; the
  services started from the slot. One `ConsumerReader` serves every row of an answer, so Heroku and the env files are
  read once per answer (`HerokuSource(memo=True)`, `EnvFileSource(memo=True)`; a door's guard keeps reading fresh).

## Data

No new file. Read: slot metadata, the doors' records, the digest manifest, the sent shares' records, each
organization's selection record, `registry/projects.json`, `config/organizations.json`. The value of a slot is read
only by `consumers`, in memory, and compared in constant time; no record, answer or log carries it.

## Wireframe

```
Keys page, Project secrets, Credential cell:

  OPENROUTER_API_KEY          SUPPORT_TOKEN              SHARED_KEY
  acme-web/prod               own-app/prod               alpha-web/prod
  [issued by a door]          [shared out to another     [shared with another project]
  openrouter . acct-a          party]                    same value as beta-api/prod/SHARED_KEY
  handed to org:acme          handed to example partner  personal
  organization: Acme Example  personal
```

## Contract

- `observatory_credentials` vault rows: `source` gains `copied`; new `copiedWith`, `sharedTo`, `context`; `consumers`
  from every guard source. Kept: `sharedWith`, `sourceContext`, `consumersTool`.
- `registry/credentials.json` project-secret rows: new `key_source_copied_with`, `key_source_shared_to`, `key_context`.
- The renamed class value is in [COMPATIBILITY.md](../../COMPATIBILITY.md) ("Keys by source: `copied` and `shared`").

## Failure behaviour

| What | Answer |
|---|---|
| a sent share's record does not read, or its folder does not list | `degraded` `share-unreadable`; its keys are not read as unshared |
| an organization's selection does not read | `degraded` `selection-unreadable` |
| `registry/projects.json` missing or unreadable | `degraded` from the registry reader; each key's `context` is `undecided` |
| a slot's value does not read | `degraded` `value-unreadable` with the slot; the value-free sources still answer |
| a consumer source cannot be read | its own code (`token-unreadable`, `config-unreadable`, `scan-missing`, ...), named once per answer |
| a recipient label that is not a plain label | shown as "a recipient whose label is not shown" |

## Tests

`observatory/engine/tests/test_key_source.py` (suite `key_source`): `OutwardShares` (a sent peer share, an issued
key shared out, a credential request into an organization's context, a hand-over, an unreadable share record),
`KeyContext` (no organizations, each project kind, a missing registry), `Consumers` (the guard's source names, every
source read and no value shown, a key no ledger names, one reader for many slots), and the surfaces carrying
`context`. Every answer is checked to carry no fixture value.
