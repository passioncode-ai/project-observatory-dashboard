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

The Cloudflare (`revoke LABEL`, `revoke --vault`), Heroku, Sentry and Prowl revokes do not run the guard yet; their
rotations already refuse through `roll_guard`. Delivery to consumers for Cloudflare and Heroku is OBS-607.

## Tests

`observatory/engine/tests/test_revoke_guard.py` (suite `revoke_guard`): the three codes, a missing, unreadable and
misshapen scan, matches by hash, label and `serves`, value sources in memory, codes on every degraded entry, the
DigitalOcean rows unchanged, and each caller refusing before its provider call, with every answer and printed line
checked to carry no fake value. Neighbours updated to give a clean scan where they test the door's own sentence:
`test_rotation_consumers`, `test_key_acts`, `test_keyserver_boundary`, `test_credential_doors`.
