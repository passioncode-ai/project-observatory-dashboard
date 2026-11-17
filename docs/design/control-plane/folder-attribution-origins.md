# Folder attribution: a remote whose user is a token (OBS-576, DEC-0177)

## What

fabric-agent-contract DEC-0177 (release `v0.1.0+2026-11-16.5`, merge commit `edf4e67a`) widens the shared credential
detector: `secretUrl` refuses a URL whose user name is a token (`tokenUserName`: 40 or more of `[A-Za-z0-9_-]` holding a
letter and a digit, a classic token's 40 hex), and `recordOrigin` records no origin for an `ssh://` or scp remote whose
user is one. It lands together with DEC-0151's secret-value rules (a provider key's prefix in its canonical case, after a
boundary, an `sk-` key only with its strength), which the engine's port did not carry yet.

## Data

- `observatory/engine/folder_attribution.py`: `token_user_name`, `secret_url` and `secret_value` (the contract's names),
  DEC-0151's `SECRET_VALUE` (no case-insensitive flag, digits as `[0-9]`), a one-pass JWT reading (`hasJwt`), and
  `record_origin`'s two DEC-0177 checks (a token in a kept URL user, between any `@`; a token as the scp user).
- `stored_remote(raw)`: what the engine keeps of a remote beside a folder. The origin `record_origin` keeps where the
  contract reads one; a remote it cannot read (an ssh alias, a host with no dot) as written unless it carries a
  credential; `None` when nothing may be kept. Idempotent.
- `store/raw/local.json` (`collectors/scan_filesystem.py`): each row's `remote` and `remotes_all` go through
  `stored_remote`. A withheld `remote` is `""` with `remote_withheld: true`; the merge reads it as a remote (the history
  is published somewhere), never as an unpublished checkout. The file carries `origins_rewritten`: how many rows stored
  before the upgrade held an origin the contract now refuses (rewritten in place when carried, re-measured clean
  otherwise). The first tick after the upgrade counts them; the next counts 0. The scan's output line says the count.
- `observatory/engine/fabric/attribution-vectors/`: the contract's `fixtures/vectors/folder-attribution.json` at
  `edf4e67a` (unchanged at `db0d8f15`), three planted userinfo colons and four long fake `sk-` hyphens written as JSON escapes (JSON-identical); `SOURCE.json` pins the
  commit and the canonical SHA-256 the test checks.

## Contract

- The contract's vectors run in `tests/test_folder_attribution.py` (135 attribution, 40 record and 28 origin cases),
  the three DEC-0177 cases and the DEC-0151 secret-value cases among them.
- No page and no MCP tool changes: the attribution answers the same; only what a record or the scan keeps of a remote
  changes.

## Failure behaviour

A remote the detector reads as carrying a credential is never written: not to `local.json`, not to the model the merge
builds. A token user name after an encoded `@`, holding `.`, or in a URL nested in a query value is not found (the
contract's known limits).

## Not here

The estate-record port (`node/estate.py`, FAC-SEM-075) and `ops_digest.py` keep their own copies of the earlier
`SECRET_VALUE`; `machine_backup.redact_url` drops a URL's user information but not an scp remote's token user.
