# Vault slots across the operator's machines (OBS-194)

One operator, several machines, one vault on each. The question "does the other machine hold
the same key, an older one, or none" had no answer short of opening both values, and opening a
value to compare it is how values end up in transcripts. This page specifies the answer: every
machine publishes keyed digests of its slots, and one view compares them.

Code: `observatory/engine/tools/vault_digests.py` (the digests, the view, the marks and the CLI
`project-observatory full vault-digests`), `survey.credentials` (the `machines` of each vault row in
`observatory_credentials`). Tests: `observatory/engine/tests/test_vault_digests.py`, over two
temporary workspaces that stand for two machines. Standard library only.

## Step 1 and step 2

| Step | What | State |
|---|---|---|
| 1 | per-machine digests, the view, the marks, `machines` in `observatory_credentials`, tests over two workspaces | this page |
| 2 | the transport between machines, and copy and move of a slot as a sealed envelope to the target node's key, journaled on both sides | waits for the contract's envelope profile and estate key, and for the S1 channel |

In step 1 a peer's manifest reaches this machine as a file the operator places in the peers
folder. Step 2 replaces that hand with the network; the manifest and the view stay as they are.

## The digest

For each slot `project/env/NAME` of the vault (the same slots `vault.py list` shows):

    digest = HMAC-SHA-256(estate key, value)

where `value` is the slot's value as the vault hands it out (surrounding whitespace removed). The
digest is keyed so that it can travel and be stored: an unkeyed SHA-256 of a short token is one
dictionary lookup away from the token, a keyed one is not without the key.

## The estate key

32 random bytes, written as 64 lowercase hexadecimal characters to `.estate-digest-key` at the top
of the vault store (beside `leaks.jsonl` and `movements.jsonl`), owner-only. It lives in the vault
so that the vault's own backup carries it and no second store exists. `write` (and `view`, which
writes first) creates it on first use. Nothing prints it: `key status` shows its id, a 16-character
HMAC of a fixed label under the key, which tells two keys apart and reveals neither.

Two machines compare only under the same key:

    project-observatory full vault-digests key export FILE        # machine A: a NEW owner-only file
    project-observatory full vault-digests key import FILE        # machine B: reads FILE (or stdin), then delete FILE

`import` reads FILE (or stdin, never an argument), refuses a different key unless `--replace` (every digest the machine publishes changes),
and refuses anything but 64 hexadecimal characters. `export` never overwrites a file. Create,
import and export are journaled in the movement journal (`vault.py movements`) with the key id.

## The manifest

`write` computes this machine's manifest and saves it owner-only at
`<state>/vault-digests/local.json` (`<state>` is `OBSERVATORY_STATE`, the workspace's `store/` by
default):

```json
{"schema": "vault-digests/1", "machine": "machine-a", "generated_at": "2026-10-26T09:00:00Z",
 "algorithm": "HMAC-SHA-256", "key_id": "0123456789abcdef",
 "slots": {"alpha-web/prod/API_TOKEN": {"digest": "<64 hex>", "rotated_at": "2026-10-20T08:00:00Z",
                                        "on_purpose": {"why": "each machine has its own token", "at": "..."}}}}
```

It holds no value and no unkeyed hash. `machine` is `--machine`, else `OBSERVATORY_MACHINE_NAME`,
else the host name; `rotated_at` is the slot's last `vault.py rotate`. A peer manifest is read only
if it parses as this schema with a plain machine name, an ISO-8601 time and a 64-hex digest per
slot; anything else is named in `degraded` and never read as agreement.

## The view

`project-observatory full vault-digests` (or `view`, `--json` for the structure) writes this
machine's manifest, reads every `*.json` in the peers folder (`--peers DIR`, else
`OBSERVATORY_VAULT_PEERS`, else `<state>/vault-digests/peers`) and gives, per slot and peer, one
state seen from this machine:

| State | Meaning |
|---|---|
| `same` | both hold the slot and the digests are equal (compared with `hmac.compare_digest`) |
| `differs` | both hold it and the digests differ; with `on purpose` when either side marked it, with `stale` when one side rotated later (the older copy is stale) |
| `only here` | this machine holds it and the peer does not |
| `missing` | the peer holds it and this machine does not |
| `unreachable` | the peer's last answer is older than `--max-age-hours` (48 by default) or was made under another estate key; the cell carries `last_answer`, the manifest's time |

When every slot is `same` (or differs on purpose) on at least one peer, no peer is unreachable and
nothing is degraded, the view is one line:

    All 12 slot(s) match on 2 machines (machine-a and machine-b; newest answer 2026-10-26T09:00:00Z).

With no peer manifest at all it says that nothing is known about other machines, never that all
match. Exit code 0 when all match, 1 otherwise, 2 for a refusal. The view never prints a digest.

## The marks

    project-observatory full vault-digests mark PROJECT ENV NAME --why "each machine has its own token"
    project-observatory full vault-digests unmark PROJECT ENV NAME

A mark is kept in the slot's own metadata (`NAME.meta.json`, `differs_on_purpose`), journaled
(`mark-differs`, `unmark-differs`), and published in the manifest, so a peer sees it too. Only a
slot this machine holds can be marked; `--why` is refused when it carries a credential shape.

A rotation marks the other copies stale without a separate step: the manifest carries each slot's
`rotated_at`, and a `differs` cell names the side that rotated later. A mark on purpose takes
precedence over staleness: a slot that is meant to differ is not reported stale.

## `observatory_credentials`

Each vault row gains `machines`: this machine, then every peer whose current manifest lists the
slot. It is read from the manifests alone (names and keyed digests), so the rule that no value
reaches that answer still holds by construction. A peer that is unreachable is left out of
`machines` and named in `degraded` (`peer-unreachable`, with its last answer), as is an unreadable
manifest and a machine that has not written its own manifest yet (`no-local-manifest`).
