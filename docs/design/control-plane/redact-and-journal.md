# One redactor (OBS-602)

## What

`observatory/engine/memory_redact.py` holds the one redactor. `redact(text, uuids=True, marker="[redacted]")` keeps
the references in `KEEP` (a session, a board request or message, a short commit), replaces the user of an http(s)
URL with the marker, then every `credential_shape` run. `scrub(value)` walks a JSON value with it. The memory filter
(`Redactor`) runs the known vault values first, then `redact`. `credential_like` is `credential_shape.shaped`.

| Site | Before | Now |
|---|---|---|
| `maintenance.redact_line` | its own URL pattern (`***@`), then `credential_shape.redact` | `redact(..., uuids=False)`, cut to 300 characters |
| `engine_update.redact_tail` | its own URL pattern only | `redact(..., uuids=False)`, then the tail |
| `ops_acts._scrub` and its two stderr lines | its own JSON walk over `credential_shape.redact` | `scrub` and `redact` |
| `retrieval_audit.scrub_*` | `Redactor`, with its own copy of the `KEEP` hold | `Redactor` over `redact` |
| `collectors/scan_lifecycle.redact` | the two kept words written as they were | the two kept words through `redact(..., uuids=False)` |

A site still decides what text it keeps (a line's length, an installer's tail, which words of a command line); it
keeps no pattern of its own.

## Data

No stored format changes. A marker reads `[redacted]` everywhere; the old `***@` of the maintenance pass and the
updater is gone. `uuids=False` is for process output, where a UUID names a simulator's device folder or a file.

## Changed answers, each a defect of the replaced redactor

- `engine_update`: a password in a URL of another scheme than http(s), a provider token, a 64-character
  hexadecimal run and a long random-looking run reached the update report.
- `ops_acts`, memory: the user of a URL without a password (`https://<token>@host`, git's form for a token) reached
  the journal and was served back; a URL with a password lost its scheme to the marker and now keeps it.
- `ops_acts`: a `session:<uuid>` reference was cut to `session:[redacted]`; memory keeps it, and so does the journal.
- `scan_lifecycle`: a token or a hexadecimal run in a script or executable path reached the process document.
- `maintenance`, `engine_update`: `***@` is now the one marker.

## Failure behaviour

`redact` raises nothing: a value that is no string, or an empty one, comes back as it was. It is idempotent: a
marker is never matched again (brackets are no userinfo characters).

## Tests

`observatory/engine/tests/test_redact_sites.py` (suite `redact_sites`) pins every site's output on one table of fake
values built from pieces, names each changed output with the old one and its reason, checks that each site asks
`memory_redact.redact` and that no site keeps a URL pattern or calls `credential_shape.redact` itself.
