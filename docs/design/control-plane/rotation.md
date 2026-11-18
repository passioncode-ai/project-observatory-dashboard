# M12: rotation never cuts a live consumer (OBS-561)

Module M12 of [CONTROL-PLANE.md](../CONTROL-PLANE.md): requirement CP-11, decision D5 (deliver, read back, then
revoke). Status: built on branch `fix/rotation-consumers`, 2026-11-14.

## What

OpenRouter's `rotate` minted a successor, delivered it to the key's one destination (a vault slot or a named local
file) and deleted the predecessor at once. A copy of the old value elsewhere (a Heroku app's config var, another
project's `.env`) kept a dead key: one production bot answered 401 for three hours after such a rotation. Cloudflare
rolls a token in place and Heroku regenerates an authorization in place; both kill the old value at the roll.

Now a door looks for every other copy of the value before it revokes, by the value itself, held in memory only:

| Source | Found by | Delivered by | Read back |
|---|---|---|---|
| `heroku` | every app the Heroku door's stashed admin tokens list (else the CLI session when the `heroku` integration is on), config vars read fresh through the door's own wire | `PATCH /apps/{id}/config-vars` with that one variable, a JSON body built in memory | `GET` the config vars, the variable compared in memory |
| `env-file` | every file in the local inventory `store/raw/env.json` (`collectors/scan_env.py`), read fresh with the scan's own parser | that variable's value replaced in place, atomically (a rename in the same folder), keeping the file's mode, quoting, comment and neighbours | the file parsed again |
| `launchd` | `use_secret.consumers()` for the vault slot | nothing: the slot is what the service reads at start | named with its `launchctl kickstart` command |

A Git-tracked env file is never written (a new key there is one commit from publication): it holds the revocation,
named with what to do.

## Data and contract

`observatory/engine/tools/key_consumers.py`:

- `find(old, sources)`: `{consumers, degraded, skipped}`. A consumer is names and places only: `kind` and `app` and
  `var`, or `path` and `var`, or `label`. `degraded` is every source or app or file that could not be read, never
  read as "no consumer". `skipped` is a source not in use here (no Heroku token and the integration off; no vault
  slot to look services up by), which holds nothing.
- `blocks(found)`: the lines that hold a revocation before anything is minted (an unreadable source, a tracked file).
- `deliver_all(found, old, new)`: one outcome per consumer: `delivered` (read back equal), `restart`, or `held` with
  the reason. `held(outcomes)` lists the ones that hold.
- `roll_guard(old, sources)`: for a door that rolls in place: the lines that refuse the roll, and the services to
  restart.
- `vault_answer(project, env, name)`: the MCP tool `observatory_key_consumers` (read-only, `degraded` always present,
  a malformed or credential-shaped slot is a typed `invalid-slot` error).

OpenRouter (`tools/openrouter.py`):

- `rotate` (`_rotate_fresh`): find the copies of the destination's value; `blocks` holds before anything is minted.
  Mint, deliver to the destination, deliver to every copy and read it back; any copy `held` leaves the row in
  `rotation_status: successor-delivered-predecessor-held` with `held_consumers`, the predecessor alive, exit 1. Only
  when every copy is delivered is the predecessor snapshotted and deleted, as before.
- A held row is finished by the next `rotate` (`_clear_hold`): the predecessor's value is found again among the
  slot's retired values (`vault.py rotate` archives them) by the hash the ledger recorded (the provider's hash is the
  value's SHA-256), delivered to its copies, then the old rotation finishes and a fresh one runs. A named-file
  destination has no retired values, so its hold stays until `--force`.
- `revoke` refuses a held row (its predecessor is live and still read).
- `issue` into a destination that already holds a value: the same check before the mint, the same delivery after;
  a copy not delivered is named in `held` and the command exits 1, because the old key it holds must not be revoked
  yet.
- `--force` (rotate and issue) goes on and journals `rotation-forced` / `issue-forced` with the copies cut.
  `rotate --dry-run` names the copies and whether the rotation would be held, and acts on nothing.

Every hold, delivery and cut is a line in the movements journal (`rotation-held`, `consumer-delivered`,
`rotation-forced`, `issue-held`, `issue-consumers-held`), names and places only.

Cloudflare (`cloudflare.py rotate`) and Heroku (`heroku.py rotate`): since OBS-607 they read `plan_roll` before the
roll. No copy: they roll in place. Copies they can all write: a successor beside the old key, delivered to each copy
and read back, then the old key revoked through the guard; a copy not delivered holds the revoke until the next
`rotate`. A source that is unreadable, or a copy they cannot write, refuses (exit 1, `rotation-held` journaled);
`--force` rolls and journals the cut. Design: [revoke-guard.md](revoke-guard.md), "The rolling doors deliver".

## Failure behaviour

- A source that cannot be read: held before anything is minted, the source named. Never "no consumer".
- A delivery refused, or a read-back that does not match: the predecessor stays alive and the row says held; the
  next `rotate` retries those copies.
- A value is never printed, logged, journaled or put in argv; a provider's answer is never echoed (the Heroku door's
  wire strips it).
- The finder reads every Heroku app's config vars into one process at each rotation, as the remote-env scan does
  once a day; nothing of them is kept.

## Not done

- Repository secrets (GitHub Actions and the like) are not a source: their API answers names only, so a copy cannot
  be found by value there. A sweep of them belongs with CP-08.
- DigitalOcean's `db rotate` already reads consumers first and was not changed.
- Revoking an issued key (`openrouter.py revoke`) still relies on the OpenRouter scan's `serves` check, not on the
  finder.

## Tests

`observatory/engine/tests/test_rotation_consumers.py` (suite `rotation_consumers`), fake Heroku and OpenRouter, no
network: the finder names a Heroku copy, an env file copy and a service, and every unread source in `degraded`; an
env file is rewritten keeping its mode and a tracked one holds; `rotate` delivers, reads back, then deletes; a
refused PATCH or a read-back mismatch keeps the old key and the row held; `revoke` refuses a held row; `--force`
cuts and journals; an unreadable source holds before the mint; a held rotation finishes once the copy is reachable;
`issue` into an occupied destination delivers to its copies; `--dry-run` acts on nothing; Cloudflare and Heroku refuse
to roll and `--force` rolls. Every case asserts the fixture values appear in no output and no journal line.
