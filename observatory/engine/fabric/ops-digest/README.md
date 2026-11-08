# Operations digests (vendored, DEC-0153)

Copied byte for byte from `passioncode-ai/fabric-agent-contract` at commit `eac1a594aa131851f079da8bb7b2149e1951f35f`,
the commit of its release tag `v0.1.0+2026-11-06.1` (DEC-0153, the contract's CT-219, its pull request #244), never
from a branch. That release is not the engine's contract pin (`fabric-contract.lock.json`): the pin moves with its own
review, so this folder is listed in `contract_lock.PIN_CHECK_EXCLUDES` and `contract_lock.VENDORED_AT_OTHER_COMMITS`
with that reason (OBS-330).

What each file is, and where it came from at that commit:

- `ops-digest-hourly.schema.json`, `ops-digest-daily.schema.json`: `schemas/`, the two closed shapes
  `ops.digest.hourly@1` and `ops.digest.daily@1`. `ops_digest.py` validates every daily line `ops.digest.record`
  receives against the daily one (which refers to the hourly one's `$defs`).
- `ops-digest.json`: `fixtures/vectors/ops-digest.json`, the reference folds' vectors (11 hourly cases and 4 daily
  ones), written by the contract's generator, never by hand. `tests/test_ops_digest.py` runs every case through the
  engine's port of `foldHour` and `foldDay` and reads every answer back through the engine's own check, so the
  engine's reading of a line agrees with the contract's.
- `positive/`, `negative/`: the `ops-digest-*` fixtures of `fixtures/positive/` and `fixtures/negative/` (a line each
  that the schema takes, and one each it refuses).
- `semantic/`: the `ops-digest-hourly-*` and `ops-digest-daily-*` fixtures of `fixtures/semantic/`, each with the rule
  codes (`FAC-SEM-166`, `FAC-SEM-167`) the contract's `opsDigestRules` reports on it. The `ops-digest-folders-*`
  fixtures of `FAC-SEM-168` are not copied: they check a service descriptor's folder declaration, which a collector
  reads and Observatory does not (`ops_digest.py` takes only that rule's `source` clause).

The rules are ported, not copied: `ops_digest.py` holds `FAC-SEM-166`, `FAC-SEM-167` and the `source` clause of
`FAC-SEM-168`, with the shared detectors they use (`secretValue` of the contract's `src/secret-shape.ts` and
`hasHomePath` of its `src/activity-rules.ts`), as `docs/specification/ops-digest.md` states them at that commit.

Do not edit these files here. When the contract changes, copy them again from a new release tag and update the tag,
the commit and the digests below; `tests/test_ops_digest.py` checks the digests.

| File | sha256 |
|---|---|
| `negative/ops-digest-daily-day-time.json` | `add5eac0ed0f9eb7d776bb45a489c6418f4e0abd28b7ca9a444cf534faed3770` |
| `negative/ops-digest-daily-hours-25.json` | `a4801f07cb25438333e018b64bc47f0659ba15ec142d6cf1a3c6e677ac0484b2` |
| `negative/ops-digest-daily-schema-slash.json` | `467a3f09ba996500ae07c53678ce1422c9c3ddfc5ab7dd95d7864feba238d81a` |
| `negative/ops-digest-hourly-half-hour.json` | `83448efd208ce7d3b4d10a70963d882af5415946680ca9714cc00d4ed71f18ed` |
| `negative/ops-digest-hourly-log-line.json` | `a9c947aad314eb95ab5ee8d0cef71e54acea26e35442dc395d286390444fa104` |
| `negative/ops-digest-hourly-negative-count.json` | `3f443647a419ebe487277cbcffa0e17b310868554e0e7d0fb71254351ca6074a` |
| `negative/ops-digest-hourly-offset.json` | `f6a2e655cb564e8bf7b761c2d0e87bc9730ee9e7e3bfa3f735fe2cf44262c769` |
| `negative/ops-digest-hourly-op-path.json` | `68048006c9fac7b8b4bb8fb29edf12d4937456c242633ab31a7bf76497cd3c24` |
| `negative/ops-digest-hourly-total-number.json` | `7513b235c5a3a10b19fa05334d10c229ede0a60b4000db04b69cd63a1a243935` |
| `ops-digest-daily.schema.json` | `a6f9cdb0e9badb04efc8519d2f1905ae1ccd3750248ea98a34f3b607ac2f44c0` |
| `ops-digest-hourly.schema.json` | `387ff91a0348476fd6b2973ff6f98429ca9c84b8dd3d6eb5154d4f9340719222` |
| `ops-digest.json` | `ed027cecf2629e26a78f4395e0508766ddfa603a0ecb0f838b3d1f47c089bb52` |
| `positive/ops-digest-daily.json` | `5f9212d5669cc49eb346018e7defa21e7e5dcdcda82065cee4e926c1f9796904` |
| `positive/ops-digest-hourly-empty.json` | `b9e35b639665bf23609205a23b89f03ff06b7e5b2e7eab4bb5b5fd24d76b4ec6` |
| `positive/ops-digest-hourly.json` | `42d89351fc8d5f3aef4078b3655e7572b3b00f77f13480d960c17160b56beb3e` |
| `semantic/ops-digest-daily-no-such-day.json` | `b40d52da3241818ac3b2feb52c03b3bb9a0847e52373602372aa73e52539fd2e` |
| `semantic/ops-digest-daily-ok.json` | `f3d029359ef5c34c555be24f7480107ea58d0901d0b26ef144fcae233b17ec03` |
| `semantic/ops-digest-daily-rise-too-few.json` | `32ec59f358d3deccad60dfb37592c8a914a2da8d1a7ed4b5ca28ad42b76fdf1a` |
| `semantic/ops-digest-daily-rose-more-hours.json` | `6d46d22be833601eaaba3c04306e2920a50407e9f453e24c7015abdac0f9027b` |
| `semantic/ops-digest-hourly-no-such-day.json` | `61cd55ab5a8be352477074072099ca6e67fceef8b6ae0a69feae0a5409aaafb6` |
| `semantic/ops-digest-hourly-not-a-rise.json` | `2176bf836d7db28edcc82942dc5821ba28dad9c5968fbb8a49b395ec7dd790c2` |
| `semantic/ops-digest-hourly-ok.json` | `c905ebb1e18848874466062f153186bf1785ffccdd951732b45221a402a977b3` |
| `semantic/ops-digest-hourly-op-credential.json` | `5a7f40881a8f811f43d7dfa18655b1128237e276230fa311ba7c9df1600ed00c` |
| `semantic/ops-digest-hourly-op-home.json` | `68b8ae16430d874cea2f4f946282e6e196e50955b7ef5ea8173c5c51e996f764` |
| `semantic/ops-digest-hourly-op-twice.json` | `dbac91a54a155f651f125b7e152ff999cc3e1732acf6621816e53f5743a555b0` |
| `semantic/ops-digest-hourly-op-users.json` | `cb3ebf171bf78b58ce20fe3becf62a7e50215f128c76f43038ed8b5697ca8893` |
| `semantic/ops-digest-hourly-rise-not-counted.json` | `a24f76a396b2d9a8eb7476f13e5b3815fb19d84ee8306f81dc6ec60d61f9beb0` |
| `semantic/ops-digest-hourly-total-not-sum.json` | `eea580cdfb125df82940941d5845f1dbd777c73ee72ebee10518777badf985c2` |
| `semantic/ops-digest-hourly-two-hours.json` | `c09503f58d42d14b1cc76cd4b117cf9b8cb7e2f736330bb065c3977d1699967c` |
