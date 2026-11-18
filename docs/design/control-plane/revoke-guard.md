# The revoke guard (OBS-594, CP-11)

Status: built 2026-11-17 on `refactor/r1-revoke-guard`. Row: OBS-594 of the refactor plan
([refactor-plan.md](refactor-plan.md), wave R1).

## What

Every key revoke runs one check before it asks the provider: `key_consumers.revoke_guard`
(`observatory/engine/tools/key_consumers.py`). It finds who still reads the key and refuses the revoke when anyone
does, or when it could not look. Before it, the OpenRouter door and the key server read the OpenRouter scan's
`serves` and skipped even that when the scan file was missing; `tools/revoke_key.py` deleted with no check; the
DigitalOcean door kept its own App Platform finder and revoked tokens with no consumer check.

## Data

The guard takes the key's fingerprints, never a printed value:

| Field | Read by | Meaning |
|---|---|---|
| `hash`, `labels`, `names` | `OpenRouterScanSource` | the provider's hash of the key, its labels and names, matched in `store/raw/openrouter.json` (`destination_hashes`, `destinations`, `keys[].serves`) |
| `value` (its own argument, not a field) | the value sources (`HerokuSource`, `EnvFileSource`, `LaunchdSource`) | held in memory, compared in constant time (`same`), never written or printed |
| `unread` | the value sources | why the door could not read the value: the guard refuses |
| `cluster` | `DigitalOceanAppsSource` | a managed database cluster; App Platform apps that bind it are consumers |

Sources are passed as objects or by name (`GUARD_SOURCES`: `openrouter-scan`, `heroku`, `env-file`, `launchd`).

## Contract

`revoke_guard(subject, key, sources, value=None)` returns `{consumers: [], degraded: [], skipped: [...]}` when the revoke may go
ahead, or raises `RevokeRefused` (a `ValueError`) with one of three codes:

| Code | When | Carries |
|---|---|---|
| `consumer-in-use` | a source found a consumer | `consumers`: each one by name and place |
| `consumer-source-unreadable` | a source could not be read (scan missing or unreadable, a value the door could not read, a source that failed) | `unread`: each entry with its own `code` (`scan-missing`, `scan-unreadable`, `value-unreadable`, `app-list-unreadable`, `source-failed`, `source-unreadable`) |
| `consumer-source-unknown` | a source name the guard does not know, an object that is not a source, or no source at all | `unread` with `source-unknown` |

Every `degraded` and `skipped` entry the finder (`find`) and the guard answer carries a `code`.

## Callers

```
openrouter.py revoke NAME ─┐
keyserver /api/revoke ─────┼─> revoke_guard(scan by hash, label, name) ─> provider DELETE
observatory_key_act revoke ┤      (key server answers 400 with {error, code, consumers, unread};
tools/revoke_key.py ───────┘       the MCP act answers the guard's code as its error code)
digitalocean.py revoke ────────> revoke_guard(value; heroku, env-file, launchd) ─> OAuth revoke
digitalocean.py db consumers ──> DigitalOceanAppsSource.apps (the door's rows, unchanged)
```

`tools/revoke_key.py` stays: it is the one route for a key minted elsewhere that only a provisioning key reaches (no
ledger row, so the door cannot revoke it). It now runs the guard before its DELETE.

## Failure behaviour

A revoke never proceeds blind: an unreadable source refuses, it is never read as "no consumer". No override is added:
none existed on any revoke path. The one person's override that exists near it, `rotate --force` on the OpenRouter,
Cloudflare and Heroku doors, already journals what it cut (`rotation-forced`, `issue-forced`).

## Not yet

The Cloudflare (`revoke LABEL`, `revoke --vault`), Heroku, Sentry and Prowl revokes do not run the guard yet. The
Cloudflare and Heroku rotations deliver to the copies they find and revoke through the guard since OBS-607 (below).

## One copy of each door helper (OBS-601)

Built 2026-11-17 on `refactor/r2-door-helpers`, row OBS-601 of wave R2.

- **`key_consumers.consumer_sources(slot, exclude)`** is where every door looks for the copies of a value (Heroku
  config vars, inventoried env files, services started from the slot). `slot` is `project/env/NAME` or a tuple
  (None when the key is not in a vault slot), `exclude` the file the key was delivered to. The OpenRouter,
  Cloudflare, Heroku and DigitalOcean doors each had their own copy; none keeps one now (the OpenRouter door keeps
  `_consumer_sources(to)`, which only turns a destination into that slot and file).
- **`key_consumers.guard_roll(slot=... | path=...)`** is the roll guard of a door that rolls a value in place: it
  reads the old value in memory (the Heroku door's vault slot, the Cloudflare door's token file), then `roll_guard`
  over `consumer_sources`. A value it cannot read refuses the roll, named by its exception's type; no source is read.
- **A code on every degraded entry** a door or a consumer source answers:

  | Source | Codes |
  |---|---|
  | `heroku` | `token-unreadable`, `app-list-unreadable`, `config-unreadable`, `config-unshaped` (skipped: `not-in-use`) |
  | `env-file` | `inventory-unreadable`, `inventory-incomplete`, `file-unreadable` (skipped: `not-in-use`) |
  | `launchd` | `services-unreadable` (skipped: `no-slot`) |
  | any source that raised | `source-failed`, in the finder and in the guard alike |
  | OpenRouter rotation | `destination-unreadable`, `predecessor-missing`, `no-successor` |
  | OpenRouter BYOK plan | `key-unreadable`, `no-workspace`, `credentials-unreadable`, `several-credentials` |

- **The key server refuses by type**, `{"error": {"code", "message"}}`, the shape the MCP tools answer. The code of an
  act's exception is `keyserver.refusal(exc)`, and the MCP key acts (`key_acts._run`) take theirs from it too:

  | Exception | Status | Code |
  |---|---|---|
  | `RevokeRefused` | 400 | the guard's own (`consumer-in-use`, ...); `consumers` and `unread` inside `error`, and `code`, `consumers`, `unread` beside it as R1 published |
  | `ValueError` | 400 | `invalid-argument`, or the text itself when it is a code (`agent-disabled`) |
  | `LookupError` | 404 | `held`, `provider-refused`, `not-found`, or the text when it is a code (`unknown-job`) |
  | `HTTPError` | 502 | `provider-refused` |
  | `RuntimeError` | 500 | `unconfirmed` |
  | anything else | 500 | `failed` |

  The request's own refusals: `unauthenticated` (401), `host-not-loopback` and `origin-not-same` (403), `stdin-only`
  (403, `put` and `rotate`), `no-such-path` and `unknown-action` (404), `not-built` (404), `invalid-framing` and
  `invalid-body` (400), `body-too-large` (413). Every message is scrubbed on the way out. The page reads
  `error.message` (and a string `error` still), keeps it as the refusal it translates, and carries `error.code` beside.
  Exit codes of the doors' commands are unchanged.

Not done here: the peer check's refusal of another OS account's connection (`peer_check.py`, shared by every local
server) still answers `{"error": "<code>"}`; the OpenRouter BYOK plan still repeats `several-credentials` once per
credential of a workspace.

## Tests

`observatory/engine/tests/test_revoke_guard.py` (suite `revoke_guard`): the three codes, a missing, unreadable and
misshapen scan, matches by hash, label and `serves`, value sources in memory, codes on every degraded entry, the
DigitalOcean rows unchanged, and each caller refusing before its provider call, with every answer and printed line
checked to carry no fake value. Neighbours updated to give a clean scan where they test the door's own sentence:
`test_rotation_consumers`, `test_key_acts`, `test_keyserver_boundary`, `test_credential_doors`.

`observatory/engine/tests/test_door_helpers.py` (suite `door_helpers`, OBS-601): no door defines `consumer_sources` or
`_roll_guard`; the one `consumer_sources` names the slot and leaves out the delivered file; `guard_roll` over a token
file and a vault slot, read in memory, refusing unread; a code on every degraded entry (by source, and by a walk of
every `degraded` literal in the doors); every key server refusal typed, each act exception's status and code, the R1
fields beside `error` kept; the MCP key acts take the same codes. Every answer is checked to carry no fake value.

## The rolling doors deliver (OBS-607)

Built 2026-11-18 on `refactor/r4-doors`, row OBS-607 of wave R4, CP-11. Before it, only the OpenRouter door delivered a
rotated key to its consumers; Cloudflare and Heroku roll a value in place (the old value dies at the roll), so a copy
found elsewhere refused the rotation.

### What

| Step | Where |
|---|---|
| Find the copies of the old value, before anything is minted | `key_consumers.plan_roll(slot=... \| path=...)`: the old value in memory, `find` over `consumer_sources`, `holds` (`blocks`: a source that could not be read, a Git-tracked file), `restart` (services from the slot), `copies` |
| No copy outside the door's own place | the door rolls in place, as before |
| Copies, every one writable | a successor beside the old key: Heroku creates an authorization with the row's scope and description (`POST /oauth/authorizations`) and proves it; Cloudflare mints a token with the preset's policies named `<name> successor` and files it through `install_issued` |
| Deliver, read back, revoke | `key_consumers.deliver_then_revoke`: `deliver_all` writes each copy and reads it back; `revoke_guard` over the value sources (Heroku, env files, vault) must find no copy of the old value; then the door's revoke (Heroku `DELETE /oauth/authorizations/{old}`; Cloudflare `DELETE` of the old token, then the successor renamed to the preset's name) |
| A copy not delivered | the revoke is held: the old key stays alive; the record (Heroku ledger row, Cloudflare token meta) is `successor-delivered-predecessor-held` with `predecessor` = `held_record` (old id, the copies by name and place, the lines that hold, a keyed digest of the old value) |
| The next `rotate` of a held key | `key_consumers.finish_held`: `recover` reads the recorded copies and finds the old value by its digest, finds its copies afresh, delivers the successor read from the door's own slot or file, revokes through the guard; nothing new is minted |
| `revoke` of a held key | refused: its old key is still read |

A new consumer source, `VaultSource` (`vault`), finds other slots of the local vault holding the same value (a key
copied into a second project's slot); the key's own slot is left out. It is in `consumer_sources` for every door and
in `GUARD_SOURCES`. Delivery is the vault's own `store_value` (the old value archived beside the slot), read back with
`vault_value`.

### Data

- `held_record`: `{predecessor, digest, consumers, held}`. `digest` is `hmac-sha256:` of the old value under this
  machine's random key (`store/state/consumer-digest.key`, owner-only, made on first use), never the value and never a
  plain hash. `consumers` keep `kind`, `app`, `app_id`, `var`, `path`, `slot`, `git`, `project` only.
- Journal lines: `consumer-delivered` per copy, `rotate` (Heroku: `authorization` and `predecessor`; Cloudflare's
  `install_issued` writes its own) and `rotation-delivered` (Cloudflare: `successor` and `predecessor`),
  `rotation-held` (`stage` `after-delivery` or `resume`), `rotation-resumed`, `rotation-forced`.

### Failure behaviour

- A source that cannot be read, or a copy that cannot be written, holds before anything is minted (exit 1), as before.
- A successor that cannot be proved or filed is deleted again (Cloudflare `discard_fresh`, Heroku `_delete_fresh`); the
  old key and its place are untouched.
- A delivery refused, a read-back that does not match, a copy the guard still finds after delivery (one that appeared
  meanwhile), a source it could not read, or a revoke the provider refused: held, named, exit 1.
- A held key whose old value no recorded copy holds any more (changed by hand) stays held (`predecessor-missing`):
  without the value its other copies cannot be looked for. `--force` revokes it anyway and journals the cut.
- `--force` with a hold before minting rolls in place as before and journals the copies it cut.

### Not done

- While a Cloudflare rotation is held, the old token keeps the preset's name, so an `issue` of the same preset in that
  window would roll the old token; `rotate` finishes the hold first.
- A Cloudflare successor renamed after its predecessor is deleted: a rename the provider refuses leaves the hold
  recorded, and the next `rotate` retries the delete (a 404 counts as gone) and the rename.

### Tests

`test_rotation_consumers` (suite `rotation_consumers`), fake Heroku apps, a fake Heroku wire and fake Cloudflare
calls, no network: a vault slot holding the value is a copy and takes the successor; Heroku and Cloudflare deliver to
a copy, read it back and only then revoke (Cloudflare renames the successor), with no regenerate or roll; a copy not
delivered holds the revoke, `revoke` refuses the held key, and the next `rotate` finishes it without minting again; a
held key whose old value is gone stays held and `--force` revokes; a copy that appears between delivery and revoke is
caught by the guard. `test_door_helpers`: every door's sources include `vault`. Every case asserts the fixture values
appear in no output, journal, ledger or record.
