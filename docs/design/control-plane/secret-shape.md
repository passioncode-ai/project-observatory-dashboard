# One secret-shape detector (OBS-595)

## What

`observatory/engine/credential_shape.py` is the one rule for "is this text shaped like a credential". The separate
detectors beside it are now calls to it:

| Site | Before | Now |
|---|---|---|
| `collectors/scan_env.py` `secret_shaped_name` | its own `SECRET_SHAPED` pattern | `credential_shape.shaped(name)` |
| `service_connections.py` `credentialish` | its own `_CREDENTIALISH` pattern | a URL or address mark, then `shaped(text, reading=STRICT)` |
| `ops_digest.py` `secret_value`, `SECRET_VALUE`, `has_jwt` | its own port of the contract's `secretValue` | the rule's `CONTRACT` reading |
| `folder_attribution.py` `secret_value` and `secret_url` | a second port of `secretValue` | the rule's `CONTRACT` reading |
| `tools/check_secrets.py` issuers, AWS id, private key block, hex | its own lists beside the rule's prefixes | `credential_shape.ISSUERS`, `issuer_of`, `AWS_ID`, `PRIVATE_KEY_BLOCK`, `HEX` |
| `tools/revoke_key.py`, `collectors/scan_openrouter.py`, `agent/providers.py` | the `sk-or-v1-` literal | `credential_shape.OPENROUTER`, `openrouter_key`, `OPENROUTER_FAMILY` |

`delegation.JS_SPACE` is the rule's `JS_SPACE` (one definition).

## Data

Three readings, named by `reading=` on `find` and `shaped`:

- `ENGINE` (the default): the shapes every door of this engine refuses.
- `CONTRACT`: fabric-agent-contract `src/secret-shape.ts` (FAC-SEM-167, DEC-0151), read as JavaScript reads it: `\s`
  is `JS_SPACE`, `.` any code unit but a line terminator, `\d` is `[0-9]`, no case folding.
- `STRICT`: `CONTRACT`, plus a contract prefix or `eyJ` at a word start and any 40+ run of the base64 alphabet, for
  strings the engine writes itself.

## Contract

The operations digest's pattern is byte-identical to the one it replaced (`CONTRACT_SECRET_VALUE.pattern`); the
contract's ops-digest vectors and folder-attribution vectors pass unchanged.

## Changed answers, each a defect of the replaced detector

- `scan_env`: a lower-case name with `sk`, `pk` or `rk` inside a word (`task_runner_concurrency_limit`) was redacted;
  a name carrying a GitHub fine-grained token, a Hugging Face token or 32 hexadecimal characters was published.
- `service_connections`: a string starting `Bearer `, or a key after `_`, passed the check that models the contract's
  reader, which refuses both.
- `folder_attribution`: Python's `\s` and `.` let `Bearer` followed by U+FEFF pass, and refused `Bearer` followed by
  U+001C, U+0085, or a space and a line separator, against the contract.

## Failure behaviour

An unknown `reading` raises `ValueError`. Nothing else changes: every refusal still names the shape, never the text.

## Tests

`observatory/engine/tests/test_secret_shape_sites.py` pins every site's answers on a table of fake values built from
pieces, names each changed answer with the old one, and checks that each site asks `credential_shape`.
