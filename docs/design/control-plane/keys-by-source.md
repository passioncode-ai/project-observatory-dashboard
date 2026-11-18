# Keys by source (M7, OBS-556, CP-10)

OBS-604 renamed the copy class to `copied`, gave `shared` its outward meaning and added each key's context and
consumers from every revoke-guard source: [keys-by-organization.md](keys-by-organization.md).

## What

Every vault slot is classed by where its value came from, so a person or an agent knows whom to ask and what a
rotation touches before acting on a key:

| class | measured from | context |
|---|---|---|
| `issued` | a door's ledger or record (`org_view.issuers`: OpenRouter ledger, DigitalOcean records, Heroku ledger, the movements journal for Cloudflare), or a credential request's `provenance.issuer` in the slot's metadata | door, account, record (and `requestId` for a request) |
| `received` | `vault.py put --from LABEL`: metadata `source: {class: received, from, at}` | `from`, `at` |
| `shared` | the same keyed digest under another project folder in this machine's manifest (`vault_digests`) | `with` (the other slots), `measuredAt` |
| `unknown` | none of the above, or metadata that does not read | `why` |

The first that holds wins, in that order. `sharedWith` is added to any class when another folder holds the same
value, so a key a door issued and somebody copied stays `issued` and names the copy. Old slots read `unknown`.

## Data

- Slot metadata `NAME.meta.json` gains `source` (only `class: received` is written). A `put` without `--from`, a
  `rotate` and the secret prompt's write drop it: a new value has a new sender or none. `copy` carries it like
  `provenance`. The journal's `put` row gains `received_from`.
- No new file. Classification reads metadata, the doors' records and the manifest's keyed digests, never a value.
- Module: `observatory/engine/key_source.py` (`classify`, `label_problem`, `consumers`).

## Surfaces

```
Keys page, Project secrets section, Credential cell:

  PARTNER_TOKEN
  alpha-web/prod
  [handed over]
  from example-org support desk

  SHARED_KEY
  alpha-web/prod
  [shared with another project]
  same value as beta-api/prod/SHARED_KEY
```

- `registry/credentials.json` project-secret rows gain `key_source`, `key_source_context`, `key_source_shared_with`
  (`source` already names the record's reader). What could not be read joins the document's `degraded`.
- `observatory_credentials` vault rows gain `source`, `sourceContext`, `sharedWith`, `consumers` (services started
  from the slot, the key consumers reader's value-free source) and `consumersTool: observatory_key_consumers` (the
  full search, which reads the value). Nothing is removed; no new tool, no new button.

## Failure behaviour

- Metadata that does not read: the slot is `unknown`, `degraded` gains `meta-unreadable` with the slot.
- A recorded label that is no longer a plain label (tampered): `unknown`, `label-refused`, the label not echoed.
- No digest manifest, or one that does not read: no slot is `shared`, `degraded` gains `shared-unmeasured`.
- A door record that does not read: named by `org_view.issuers` in `degraded`; its keys read `unknown`.
- `--from` refused (exit 2, nothing written): empty, over 80 characters, a control character or line break, shaped
  like a credential, or equal to or containing the value.

## Limits

- `shared` compares vault folders, not projects: two folders of one project with the same value read `shared`.
- The manifest is as fresh as its last `vault-digests write`; `measuredAt` says when.
- Shares received over the node (`node/shares/`) stay sealed outside the vault and are not classed here.
- The Keys page has no tabs per class yet; the chip and its context line are in the Credential cell.

## Tests

`observatory/engine/tests/test_key_source.py`: each class with its context (`test_key_classes`), issued and shared
together, no manifest, `--from` round trip and refusals, unreadable and tampered metadata, the
`observatory_credentials` and registry rows; every output, the journal and the metadata checked clean of the fake
values.
