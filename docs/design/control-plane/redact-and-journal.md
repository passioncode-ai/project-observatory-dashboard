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

# One journal writer (OBS-614)

## What

`observatory/engine/journal_writer.py` appends records to a journal. `append(path, records, rotate=False,
fsync=False, sort_keys=False, ensure_ascii=False)` writes each record as one JSON line; `append_lines(path, lines,
...)` takes lines already encoded (a module with its own canonical form). The rule:

- one line per record, every record of a call in one `write` on an `O_APPEND` descriptor;
- the folder created 0700, the file created 0600, an existing loose file tightened (`osprivacy.make_private`);
- opened with `osprivacy.NOFOLLOW`, and only a regular file is written;
- rotation only when the caller asks (`rotate=True`), and then by `log_policy.rotate`, the one rotation rule; a
  journal read back as a record (review decisions, configuration undo) does not ask, and `log_policy.sweep` bounds
  the logs folder;
- a failure raises `OSError` (a line break inside a record raises `ValueError`, before anything is written), and the
  caller keeps its own policy: swallow it, name it in `degraded`, or let it stop the act.

```
caller --records--> journal_writer.append --lines--> append_lines
                                                      | mkdir 0700
                                                      | rotate? -> log_policy.rotate
                                                      | open O_APPEND|O_CREAT|NOFOLLOW 0600
                                                      | regular file? tighten loose mode
                                                      | one write, fsync? , close
```

## Moved onto the writer

Each keeps its serialization (`sort_keys`, `ensure_ascii`) and its failure policy; its existing tests are unchanged.

`configuration_door._journal`, `workspace_profile._log`, `engine_update.log`, `retrieval_audit` (the receipt row),
`memory_access._journal`, `update_events.emit` (with `rotate=True`, as it rotated before), `ops_acts._journal`,
`tracked._journal`, `review_policy.append_journal` (`append_lines`, `fsync=True`), `store/workflow._journal_known`,
`store/forget._journal`, `node/shares._append`, `tools/daily_doctor._append`, `tools/cleanup.journal`,
`tools/scrub_companion.journal`, `tools/hung_commands.run`, `tools/session_start` (the
hook log and the seen log), `store_faults.record`, `companion_faults.append`.

What each gained: `review_policy`, `retrieval_audit`, `memory_access`, `store/workflow` and `store/forget` no longer
follow a symbolic link; `daily_doctor`, `cleanup`, `scrub_companion`, `hung_commands`,
`session_start`, `store_faults` and `companion_faults` create their file owner-only instead of tightening it after the
write or never.

## Not moved, and why

| Module | Why it keeps its own append |
|---|---|
| `session_reload._append` | trims the file to its newest lines before each append; `log_policy` keeps generations instead, so folding it changes what is kept |
| `secret_prompt.journal` | its own size rule (one `.1` generation at `JOURNAL_MAX_BYTES`) |
| `why_asks` | rotates under its own lock (taken without waiting, size checked again inside) so two sessions rotate once |
| `node/org.journal` | keeps the newest 200 lines past `JOURNAL_BYTES` and logs each line too |
| `usage_analytics` queue | a send queue, not a journal: refused when full and counted, compact separators, consumed by the sender |
| `tools/vault._append_private` | the vault boundary: a non-blocking open, an exclusive lock, the private folder chain checked from a root, fsync |
| `tools/keyserver` audit | a non-blocking open and its own scrub per field (the caller's UUID kept) |
| `fabric_service` | a locked store opened in binary, not a journal |
| `skill/plugins/observatory-log/hooks/prompt-why.py` | ships in the plugin, which cannot import the engine |
| `tools/disk_guard.journal` | its full-disk tests inject the failure through `builtins.open` and `os.chmod`; moving it needs those tests rewritten, which this change does not do |
| `tools/ssh_keys`, `tools/install_launchd.prepare_logs`, `tools/scheduled.log` | no journal: an SSH config, launchd's output files, a child's output descriptor |

Each row with a rotation of its own is a candidate for `log_policy` with its own size and generations; that is a
behaviour change with its own tests, left for a later row.

## Tests

`observatory/engine/tests/test_journal_writer.py` (suite `journal_writer`): one line per record in order, the
caller's encoding options, a line break refused with nothing written, nothing written for no records, 0600 and 0700,
a loose file tightened, no write through a symbolic link or into a folder, rotation only when asked and then by
`log_policy`, fsync on request, one `write` per call, and that every moved module keeps no `O_APPEND` or `"a"` open
of its own.
