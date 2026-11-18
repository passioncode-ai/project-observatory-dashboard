# Keys by organization (OBS-604, OBS-627, OBS-628, CP-10)

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

## The Heroku user in the organization rules (OBS-628)

`config/organizations.json` gains `match.heroku_users`: the Heroku user an organization's keys are issued by, named by
the email or the account id the Heroku door recorded beside its admin stash (`heroku-admin/<label>.meta.json`,
written by `heroku.py stash`). The Heroku ledger row of a key issued by the user names that stash (`admin`); a row
that names none is read against every stashed user.

```
org accounts                                   org keys --org acme
  heroku user your Heroku user: 1 key(s)         organization  acme-web/prod/HEROKU_TOKEN:
    [rule]  match.heroku_users names it            the project is acme's (rule ...), and it was issued in
                                                   heroku account your Heroku user, which is acme's
```

- `organizations.ACCOUNT_RULES` gains `heroku_users` for the provider `heroku-user`, an account-only rule like
  `openrouter_accounts`: it decides the account's organization and the issuer side of its keys, never a project.
- `org_view.issuers` gives the Heroku user's keys the rule `heroku-user` and the user's email and id;
  `org_view.issuer_organization` is the one reading of an issuer's organization (`org keys`, the not-movable admin
  tokens, the foreign-issuer finding). A Heroku user no rule names keeps the default organization, as before; two
  organizations naming it is a conflict, as for any account.
- `org accounts` names the account `heroku:user` by the same keys, so its row reads `rule` with the rule's why.
- The configuration door declares `organizations.organizations` as one object, so the rule needs no new door key;
  `organizations.validate` accepts it as a list of strings, like every `match` rule.

## A key from another organization's account (OBS-627)

`org_view.foreign_issuers()` compares, for every vault slot with a door record of its issuer and a project whose
organization is decided, the issuer account's organization (`issuer_organization`) with the project's. A different
organization, or an account no organization claims, is a row, unless:

- the issuer is listed in `match.shared_issuers` of the organization that owns it (or of any organization, for an
  account no organization claims): the operator's agents running on one organization's account in every
  organization;
- the top-level `partners` names the issuer as a partner of that project.

Not compared: a project in conflict, Not ours or unknown to the registry; a key put by hand (its issuer is unknown);
an account two organizations claim. Each is already a decision in `org keys` or `org accounts`.

```
Findings, warning:
  co-web/stage/OPENROUTER_API_KEY was issued in another organization's account
  detail  belongs to project:co-web, which is company's; issued in the openrouter account main
          (openrouter:account:main), which is person's (openrouter ledger (...))
  action  issue its replacement from an account of Example Company:
          `python ".../tools/openrouter.py" issue --account company-or --name co-web-openrouter_api_key
           --to vault:co-web/stage/OPENROUTER_API_KEY`; move its consumers, then revoke this key; or declare
          the account in match.shared_issuers, or the partner under partners
```

The finding `org.foreign_issuer` (`tools/organization_findings.foreign_issuer_findings`, called by
`build_findings`) carries the slot as `credential:vault/<slot>`; its step names an account of the project's organization
when a rule names one (`openrouter_accounts`, `digitalocean_teams`), else a placeholder. A door record that does not
read is named in the view's `degraded` and costs the board nothing else.

## Tests

`observatory/engine/tests/test_organizations.py` (suite `organizations`), class `ForeignIssuers`: a key from another
organization's account named both ways with its step; an issuer shared on purpose, and one an organization cannot
declare for another; an account no organization claims, and a partner of the project and of another; the board's
row with no value; silence without organizations and the validation of the new entries.

`observatory/engine/tests/test_organizations.py` (suite `organizations`), class `HerokuUsers`: without the rule the
key stays ambiguous and the user personal; the rule by email (any case) and by id attributes the key, the account and
the admin token's reach; a ledger row without `admin`; a user two organizations name; the rule's validation.

`observatory/engine/tests/test_key_source.py` (suite `key_source`): `OutwardShares` (a sent peer share, an issued
key shared out, a credential request into an organization's context, a hand-over, an unreadable share record),
`KeyContext` (no organizations, each project kind, a missing registry), `Consumers` (the guard's source names, every
source read and no value shown, a key no ledger names, one reader for many slots), and the surfaces carrying
`context`. Every answer is checked to carry no fixture value.
