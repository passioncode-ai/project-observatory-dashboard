# The engine's other ports of the credential detector (OBS-587, DEC-0151, DEC-0177)

## What

OBS-576 brought `folder_attribution.py` to fabric-agent-contract DEC-0151 (a provider key's prefix in its canonical
case, after a boundary, an `sk-` key only with its strength, a one-pass JWT reading) and DEC-0177 (a URL's user name
that is a token). Three places the engine reads credentials kept the earlier rules; this change closes the list
`folder-attribution-origins.md` names under "Not here".

## Data

- `node/estate.py` `secret_value` (FAC-SEM-075, and `node/node_summary.py`, which imports it): no copy of the pattern
  any more; it answers with `folder_attribution.secret_value`, the port the contract re-syncs it with. The contract's
  estate rules read no URL (`src/estate-record-rules.ts` calls `secretValue` only), so DEC-0177 changes nothing there.
- `ops_digest.py` `SECRET_VALUE`, `has_jwt`, `secret_value` (FAC-SEM-167 on an operation's name, FAC-SEM-168's
  `source` clause; `ops_digest_folders.py` and `machine_health.py` read them): its own port of the DEC-0151 pattern,
  because this module reads `\s` as `JS_SPACE` and `.` as JavaScript does (its docstring), which the folder-attribution
  port does not. `ops_digest_folders.py` tells a long-run-only `dir` with `has_jwt`, as the contract's `hasJwt`.
- `machine_backup.redact_url` (a checkout's `remotes` in a machine bundle's manifest): a URL still loses its user
  information; the scp form `user@host:path` now loses a user that is a token (`token_user_name`), is shaped like a
  credential or carries a password, and keeps a plain one (`git@host:path`). Idempotent.

## Contract

The vendored ops-digest files (`fabric/ops-digest/`) are byte-identical to the contract's current fixtures, so their
pin does not move; every case still passes. Both ports agree with `folder_attribution.secret_value` over every string
of the contract's folder-attribution vectors (`fabric/attribution-vectors/`), checked in `test_node_estate.py` and
`test_ops_digest.py`. The estate fixtures in `fabric/node-vectors/` are unchanged at the contract's head.

## Failure behaviour

A name or label DEC-0151 no longer reads as a key is accepted: `ops.risk-assessment-completed`, a lower-case `source`
holding `sk-` (it can hold no upper-case letter and no 32 characters after `sk-`). A lower-case label holding a Slack
or GitLab key's shape after a boundary is still refused. A remote redacted to `host:path` is cloned on restore with
the default ssh user.

## Tests

`test_node_estate.SecretValueDec0151`, `test_ops_digest.TheDetectors` (DEC-0151 cases, `has_jwt`, agreement),
`test_ops_digest_record` (the `source-refused` case moved to a label the rules still refuse),
`test_machine_backup` (`test_an_scp_remote_keeps_a_plain_user_and_loses_a_token_user`, and the manifest of a
checkout with an scp token-user remote holds no token).
