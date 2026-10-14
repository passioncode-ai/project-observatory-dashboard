# Leak scan: every agent's transcript homes (I-5)

## State now

Branch `iter1/I-5`, pull request #279, not merged.

| Round | Outcome |
|---|---|
| Review round 1 | Failed the unit and product tiers; fix cycle 1 followed |
| Review round 2 | Failed unit and product again; seam passed and the round-1 fixes held |
| Fix cycle 2 | Pushed on the same branch, with no force-push. origin/main had not moved |
| Review round 3 | Failed on one MEDIUM, a regression against main: damaged tables were skipped silently. Seam and product passed |
| Fix cycle 3 | Pushed on the same branch, with no force-push. origin/main had not moved |
| Review round 4 | Failed on two MEDIUM findings; every fix-cycle-3 item was confirmed and seam passed |
| Fix cycle 4 | Pushed on the same branch, with no force-push. origin/main had not moved |
| Review round 5 | Failed on two MEDIUM findings in paths cycle 4 did not touch; every fix-cycle-4 item was confirmed and seam passed |
| Fix cycle 5 | Pushed on the same branch, with no force-push. origin/main had not moved |
| Review round 6 | Failed on one MEDIUM finding; every F1–F6 item was confirmed and seam passed |
| Fix cycle 6 | origin/main (#281, #277) merged first, cleanly; pushed with no force-push |
| Review round 7 | **Passed**, with no MEDIUM or HIGH finding: 161/161 path probes, 56 seeded suites on the base, the head and the merge. Four LOW findings |
| Polish cycle | The four LOW findings closed; origin/main had not moved; pushed with no force-push |
| Review round 8 | **Passed**, CI green 8/8: R2 is met. One LOW finding left to fix before the merge |
| Round-8 fix | `sqlite_uri` keeps `..`; pushed, with no force-push |
| Rebased onto #248 | #248 merged as `7e078dc` on main. This branch was rebased onto it as one squashed commit, replacing the cycle commits and the two main merges, and pushed `--force-with-lease` against `bb16cf3f` |

Next task: the coordinator's confirming read, then merge.

**The rebase onto #248.** It had two conflicts, both foreseen:

- **`tools/scan_leaks.py`, the STATE write.** Kept #248's `_write_json` (which passes the
  document through `_clean`) and added `"signatures"` to it. `"sqlite_signatures"` merged
  cleanly in the same call.
- **`observatory/engine/SOURCE-INVENTORY.json`.** Took main's side and regenerated it with
  `tools/update_inventory.py`; `--check` passes.

Everything else merged cleanly, including `tests/test_leak_coverage.py`, which now holds #248's
tests and these together, and the `paths.sqlite_uri` fix (`Path(path).absolute().as_uri()`).

**F7 (FTS5 double count) is now OBS-95 on the backlog** (reserved with `agent_sync.py reserve
OBS`). It is not a few lines, and nothing tests it yet.

**Follow-up, out of scope here.** #248 replaces stat-then-open with an `O_NONBLOCK`,
`O_NOFOLLOW` and descriptor-path check for env reads; it is now on main. Transcript files should
be opened through the same helper.

## Behavior

`tools/scan_leaks.py` read only the configured `sessions` source (Claude Code). A value
echoed into a Codex rollout was neither a sighting nor named in `not_scanned`.
`paths.transcript_homes()` now names each home with the tool's own evidence:

| Agent | Location | Evidence |
|---|---|---|
| Claude Code | the configured `sessions` source; `$CLAUDE_CONFIG_DIR` (`~/.claude`) `history.jsonl` | `docs/ONBOARDING.md` (`sources.sessions`); the strings of Claude Code 2.1.296 (`CLAUDE_CONFIG_DIR`, `"history.jsonl"`) |
| Codex | `$CODEX_HOME` (`~/.codex`) `sessions/`, `archived_sessions/`: `rollout-*.jsonl`, `rollout-*.jsonl.zst`; `history.jsonl` | openai/codex `codex-rs/rollout/src/lib.rs` (`SESSIONS_SUBDIR`, `ARCHIVED_SESSIONS_SUBDIR`), `compression.rs` (`.zst`); the strings of codex-cli 0.162.1 (`history.jsonl`) |
| Codex databases | `$CODEX_SQLITE_HOME` (default `$CODEX_HOME`): `state_*`, `logs_*`, `thread_history_*`, `memories*`, `queue_*`, `goals_*`, `agent_message_board_*` `.sqlite` | openai/codex `codex-rs/state/src/sqlite.rs` `*_DB_FILENAME` (lines 34-39, 107-109) and `lib.rs` `SQLITE_HOME_ENV` (line 140), commit `3620b2ca`; `ext/agent-message-board/README.md` line 9 (same SQLite home); schemas from the strings of codex-cli 0.162.1: `thread_items.item_json`, `thread_turns.error_json`, `logs.feedback_log_body`, `stage1_outputs.raw_memory`, `queued_items.payload_json`, `threads.title`, `posts.payload`/`request`/`body_search` are `TEXT` |
| Hermes | `$HERMES_HOME` (`~/.hermes` + `$HERMES_DATA_DIR_SUFFIX`), its root and `profiles/<name>/`: `state.db`, `sessions/*.json`, `sessions/*.jsonl` | hermes-agent 2026.9.21 `hermes_constants.py` (`get_hermes_home`, `get_default_hermes_root`), `hermes_state.py` (`DEFAULT_DB_PATH`), `hermes_state_common.py` (schema: `sessions.last_activity_at`); upstream `hermes_constants.py:51-59` at `0006a5e2` (`HERMES_DATA_DIR_SUFFIX`) |
| opencode | `$XDG_DATA_HOME/opencode` (`~/.local/share/opencode`): `opencode*.db` or `$OPENCODE_DB`, legacy `storage/**/*.json` | opencode docs `troubleshooting.mdx` (`opencode.db`, `OPENCODE_DB`); the strings of opencode 1.18.10 (schema: `part`, `message`, `session` with `time_updated` and `data text`) |

The schemas were read from the installed binaries and source code. No database on this machine
was opened.

**Files.** A `.jsonl` is read from its last offset. Any other file is read whole again when its
`[size, mtime_ns]` changes. Before, every target resumed from its byte offset, so a file
rewritten in place skipped a value written before its old end.

**Databases.** These are read by `scan_sqlite`, 500 rows per statement:

- New rows are read up to the highest rowid at signature time.
- The rows below the mark are read again whole in three cases:
  - the inode changed;
  - the count, the rowid sum (modulo 2147483647) or the sum of squares (modulo 1000000007) is not
    the previous value plus the new rows;
  - the previous newest row's digest changed.
- With an update column, the finest-grained one is used (`updated_at_ms`, `time_updated`,
  `updated_at`, `last_activity_at`, `source_updated_at`). Rows above its last maximum are read
  again. A row at that maximum is read again only when its digest is not the recorded one. Up to
  64 such digests are kept; past 64 rows one digest of the whole set is kept, and the set is read
  again only when it changes.
- A BLOB maximum, or text that is not UTF-8, is kept as a digest, so the state file always
  serialises. Text that is not UTF-8 is read as its bytes.
- Only a table declared `WITHOUT ROWID`, or one whose own columns take all three of `rowid`,
  `_rowid_` and `oid`, is read whole, and such a table is re-reported every pass. Rows are
  paged on whichever of the three names is free. Any other error from a table (a damaged
  page, a missing module) makes the store "read only in part": a hole named in `not_scanned`.
  The other tables are still read, and the marks do not move.
- Columns are chosen by SQLite's TEXT/BLOB affinity rule, so `VARCHAR(255)` and `NCHAR(10)` are
  read.

The signature is kept in `sqlite_signatures` beside the mark.

**Coverage.** `coverage.transcript_homes` reports `absent`, `scanned`, `partial` or `unreadable`
for each home, and `coverage.transcript_stores` reports the rows each database read.

- A hole belongs to its nearest home. A locked folder under `~/.claude/projects` makes that home
  `unreadable` and `~/.claude` `partial`.
- A profile that is a symbolic link (a loop included) or not a folder is named in `not_scanned`.

## Review round 1 → fix cycle 1

| Item | Fix | Test (`tests/test_leak_coverage.py`, `TranscriptHomes`) |
|---|---|---|
| U1 deadline-cut home reported `scanned` | `home_coverage(notes, unread)` → `partial` | `test_a_home_the_deadline_cut_is_partial_not_scanned` |
| U2 symlinked profile / link loop dropped | `transcript_homes()` notes each odd profile child | `test_a_symlinked_profile_and_a_link_loop_are_named` |
| U3 rowid mark misses rows | `scan_sqlite` signature + update column | `test_a_store_replaced_by_one_with_a_higher_rowid_is_read_again`, `test_a_reused_rowid_of_the_deleted_newest_row_is_read`, `test_a_row_rewritten_in_place_is_read_again`, `test_a_hermes_session_updated_in_place_is_read_again`, `test_a_row_inserted_below_the_mark_is_read` |
| U4 long read cursor | one statement per 500-row page; side effects documented | `test_a_store_larger_than_a_page_is_read_to_the_end` |
| R2d, R2e, R3′, R4 reverts stayed green | — | `test_r2d_…`, `test_r2e_…`, `test_r3_…`, `test_r4_…` |
| P1 missing homes | Claude and Codex `history.jsonl`, Codex `*.sqlite`, `CODEX_SQLITE_HOME` | `test_codex_and_claude_histories_and_codex_databases_are_read`, `test_codex_sqlite_home_and_hermes_data_dir_suffix_are_followed` |
| P2 environment and `HERMES_HOME` | ONBOARDING | — |
| P3 `HERMES_DATA_DIR_SUFFIX` | honoured | `test_codex_sqlite_home_and_hermes_data_dir_suffix_are_followed` |
| P4 finding severity, upgrade cost | CHANGELOG, COMPATIBILITY | — |

**Correction to fix cycle 1's revert claim (P5).** That cycle's table listed "P1 Codex
databases off" as a single revert, which removed every Codex glob at once. That showed the globs
were guarded as a group, not each pattern: removing `queue_*`, `goals_*` or one other pattern
alone stayed green. Its reverts also ran in memory only. Fix cycle 2 guards each pattern with its
own test and runs every revert on disk.

## Review round 2 → fix cycle 2

The tests were written first. Before the change, the eight new tests ran as follows:

- **Red (6):** F1, F2, the column order, F3, the globs and F7 (5 failures, 1 error). F1's error
  was the reported crash, `TypeError: Object of type bytes is not JSON serializable`.
- **Green (2):** the paging-statement test and the `CLAUDE_CONFIG_DIR` test. Those behaviours
  already worked; the tests exist so the planted reverts below turn red.

| Item | Fix | Test (`TranscriptHomes`) |
|---|---|---|
| F1 BLOB update column broke the state file | `_plain()` keeps a digest for non-scalar values | `test_a_blob_in_an_update_column_does_not_break_the_scan` |
| F2 edit at the update maximum missed | finest column first; digests of the rows at the maximum | `test_an_edit_that_keeps_the_update_column_at_its_max_is_read_once`, `test_the_finest_update_column_is_preferred` |
| F3 CHANGELOG overclaimed delete + insert | rowid sum in the signature; claims narrowed; weekly-only cases listed | `test_a_changed_rowid_set_with_the_same_count_is_read` |
| F4 Codex `thread_items` effectively weekly | listed under Not covered, in ONBOARDING and the CHANGELOG | — |
| F5 reverts that stayed green | — | `test_each_page_is_its_own_bounded_statement`, `test_claude_config_dir_moves_the_prompt_history`, `test_every_codex_database_name_is_globbed` |
| F6 Codex agent message board not globbed | `agent_message_board_*.sqlite`; `~/.codex/sqlite/*.db` listed as not covered | `test_every_codex_database_name_is_globbed` |
| F7 a nested home's hole marked its parent unreadable | the nearest home takes the hole; its parents are `partial` | `test_a_hole_in_a_nested_home_leaves_the_outer_home_partial` |
| F8 run record | this section and the P5 correction above | — |

### Planted reverts, on disk

Each revert writes the reverted source to disk, runs its guarding tests in a fresh process with
`-B` and an empty `PYTHONPYCACHEPREFIX`, and restores the original bytes. The 22 guarding tests
were green before the reverts and again after, and both source files ended byte-identical.

**Lesson.** The first harness run let the U2 revert pass. A `.pyc` is trusted when the source's
mtime (in seconds) and size match, and the U1 and U2 reverts were both 12 characters shorter and
written within one second, so the U2 run executed U1's cached bytecode. An on-disk revert harness
needs a bytecode cache it cannot reuse.

| Revert | Red |
|---|---|
| F1 BLOB update value kept raw | 1/1 (error) |
| F2 rows at the old max not checked | 1/1 |
| F2 at-max digests ignored (reported every tick) | 1/1 |
| F2 seconds before milliseconds | 1/1 |
| F3 rowid sum off | 1/1 |
| F5 one statement, no LIMIT | 1/1 |
| F5 `CLAUDE_CONFIG_DIR` ignored | 1/1 |
| F5 glob `queue_*` off | 1/1 |
| F5 glob `goals_*` off | 1/1 |
| F6 glob `agent_message_board_*` off | 1/1 |
| F7 every holder unreadable | 1/1 |
| U1 deadline cut not `partial` | 1/1 |
| U2 profile notes off | 1/1 |
| U3a inode check off | 1/1 |
| U3b newest-row digest off | 1/1 |
| U3c update-column read off | 2/2 |
| U3d count and rowid sum off | 1/1 |
| U4 stop after the first page | 1/1 |
| R2d data-folder listing note off | 1/1 |
| R2e profiles listing note off | 1/1 |
| R3′ unchanged-file skip off | 1/1 |
| R4 table mark not kept | 1/1 |
| P1 Codex `history.jsonl` off | 1/1 |
| P1 Claude `history.jsonl` off | 1/1 |
| P1 Codex databases off (all globs) | 1/1 |
| P1 `CODEX_SQLITE_HOME` ignored | 1/1 |
| P3 `HERMES_DATA_DIR_SUFFIX` ignored | 1/1 |

U3d now reverts the count and the rowid sum together. Since the sum was added, either one alone
catches a delete followed by an insert under a new rowid.

## Review round 3 → fix cycle 3

The tests were written first. Before the change, 7 of the 8 new tests were red (8 failure
records, because the collision test runs two subtests). The eighth,
`test_a_real_without_rowid_table_is_still_read`, was already green: it guards the WITHOUT ROWID
path that the fix narrows.

| Item | Fix (`tools/scan_leaks.py`) | Test (`TranscriptHomes`) |
|---|---|---|
| N1 a damaged table was skipped silently (regression against main) | `read_table`: whole-table path only for a declared `WITHOUT ROWID`; every other `sqlite3.Error` becomes a hole (`PARTIAL`, error type only); the fallback no longer breaks quietly; `text_factory` keeps non-UTF-8 text as bytes; `_plain` digests surrogate text | `test_a_damaged_page_is_a_hole_not_a_clean_table`, `test_a_damaged_hermes_state_db_is_unreadable_end_to_end`, `test_invalid_utf8_in_the_newest_row_is_read_not_skipped`, `test_invalid_utf8_in_the_update_column_keeps_the_scan_and_its_state`, `test_a_real_without_rowid_table_is_still_read` |
| L1 more than 64 rows at the maximum reported every tick | `at_max_set`, one digest of the set (`_set_digest`) | `test_a_bulk_update_past_the_digest_cap_is_reported_once` |
| L2 rowid-sum collisions | `sq`, the sum of squares modulo 1000000007; residual collisions listed as weekly-only | `test_a_rowid_sum_collision_is_read` ({2,5}→{3,4}; r→r+2147483647) |
| L3 mixed-type update column | documented as weekly-only | — |
| L4 "a writer waits for at most one page" | corrected in CHANGELOG, ONBOARDING and the docstring; WAL modes stated (Codex `codex-rs/state/src/sqlite.rs:391`; opencode `journal_mode = WAL` in 1.18.10; Hermes WAL by default, configurable) | — |
| L5 `agent_message_board_*` missing from the docstring and comments | added | — |
| L6 pre-existing limits | `VARCHAR(n)` fixed (`_holds_text`); rowid ≤ 0 and WITHOUT ROWID repeats listed as follow-ups | `test_a_varchar_column_is_read` |

**Planted reverts, cycle 3.** All on disk, with `-B` and an empty `PYTHONPYCACHEPREFIX`. Each
of these 7 new reverts was red:

| Revert | Red |
|---|---|
| N1 a damaged table swallowed | 2/2 |
| N1 `WITHOUT ROWID` never recognised | 1/1 |
| N1 strict UTF-8 decoding | 2/2 |
| N1 surrogate text kept raw in the state | 1/1 (error) |
| L1 no set digest past 64 rows | 1/1 |
| L2 sum of squares off | 2/2 subtests |
| L6 exact type names | 1/1 |

The 27 reverts of fix cycle 2 were run again against this code, and all were red. Two of them
changed definition because the sum of squares now backs up the rowid sum:

- F3 reverts the sum and the sum of squares together;
- U3d reverts the count and both sums together.

That makes 34 reverts in all. The 30 guarding tests were green before and after, and both
sources ended byte-identical.

## Review round 4 → fix cycle 4

The tests were written first. Before the change, 5 of the 7 new tests were red (3 errors and 2
failures). The other two, the forced error that quotes a cell and the wording of the "read only
in part" note, were already green: they guard behaviour fix cycle 3 had, so their reverts turn
red below.

| Item | Fix (`tools/scan_leaks.py`) | Test (`TranscriptHomes`) |
|---|---|---|
| M1 a name that is not UTF-8 crashed the whole scan (regression from cycle 3) | the per-table handler also catches `UnicodeError`, and the store handler too; a column whose name is not UTF-8 is left out and named, the rest of its table read; identifiers quoted by `_q`; names in the report pass through `_shown`; `main()` turns anything one store raises into a hole | `test_a_table_name_that_is_not_utf8_is_a_hole_not_a_crash`, `test_a_column_name_that_is_not_utf8_leaves_the_rest_of_the_table_read`, `test_a_store_that_raises_anything_is_a_hole_and_the_report_is_written` |
| M2 "error type only" had no guard | — (the rule already held) | `test_a_hole_never_carries_the_error_message`: a forced `OperationalError` whose message quotes the planted value; the value is not in leak-scan.json, the findings or the state |
| L3 generated columns blinded their table | digests and reads select the plain columns of `table_xinfo` (hidden = 0), never `*` | `test_a_generated_column_does_not_blind_its_table` (tables `g` and `h`, both read) |
| L5 a comment fooled the WITHOUT ROWID check | `_without_rowid`: `pragma_table_list.wr` (SQLite 3.37+), else the CREATE statement with its comments removed | `test_a_trailing_comment_does_not_hide_without_rowid` |
| L6 three items missing from Not covered | ONBOARDING, CHANGELOG | — |
| L4 a table that can never be read keeps every pass complete | recorded here and in ONBOARDING; behaviour unchanged | — |
| L7 the wording of the partial-store note | — | `test_a_partial_store_note_says_read_in_part_with_the_table_and_type` |

**Why M2 has its own forced input.** With L3 fixed, the verifier's input (a generated column
`json_extract('{}', data)`) is never evaluated, so it cannot guard the rule any more. It stays as
the L3 test, which asserts that the value is nowhere in the report. M2 forces an error whose
message quotes the value.

**Planted reverts, cycle 4.** All on disk, with `-B` and an empty `PYTHONPYCACHEPREFIX`:

| Revert | Red |
|---|---|
| M1 `UnicodeError` not caught per table | 1/1 |
| M1 a column named in bytes not left out | 1/1 |
| M1 no catch-all around one store | 1/1 (error) |
| M2 the error message kept in the hole | 1/1 |
| L3 digests over `SELECT *` | 1/1 |
| L5 raw CREATE text, no `pragma_table_list` | 1/1 |
| L7 every store note says "would not open" | 1/1 |

The 34 earlier reverts were re-run against this code, and all were red: 41 of 41 in all. The 37
guarding tests were green before and after, and both sources ended byte-identical.

**Not planted.** The store-level `except UnicodeError` has no revert: no input reaches it once the
per-table handler and the catch-all in `main()` are in place.

## Review round 5 → fix cycle 5

The tests were written first. Before the change, 7 of the 8 new tests were red (9 failure
records, because the fallback-parser test runs one subtest per statement). The eighth, the
`"` doubling in `_q`, was already green and guards its revert. A ninth test, for `_mod`, was
added before the reverts: once the rowid alias is fixed, no input reaches its guard.

| Item | Fix (`tools/scan_leaks.py`) | Test (`TranscriptHomes`) |
|---|---|---|
| F1 SQLite's message leaked through the store-level handlers | type only for a store that will not open, a malformed schema, and the catch-all in `main()` | `test_a_schema_error_quoting_a_value_never_reaches_the_report` (CREATE text ending `'<value>'`, via writable_schema), `test_an_open_error_quoting_a_value_never_reaches_the_report`; both check the report, the findings, the state and stdout |
| F2 a column named `rowid` shadowed the rowid | `_rowid_alias` picks `rowid`, `_rowid_` or `oid`, whichever no declared column takes, for every page, digest and aggregate; a table taking all three is read whole; `_mod` counts a non-integer as 0 | `test_a_column_named_rowid_does_not_shadow_the_real_rowid` (`rowid TEXT` NULL; `rowid INTEGER` ×601 duplicates; `rowid` holding text; full, then incremental), `test_a_table_shadowing_every_rowid_alias_is_read_whole`, `test_the_rowid_sum_helper_is_type_safe` |
| F3 the catch-all dropped sightings and said "would not open" | `scan_sqlite(…, hits=found)` keeps the caller's dict; "a SQLite store stopped while reading (<Type>)" (`STOPPED`) | `test_a_store_that_stops_midway_keeps_its_sightings_and_says_so` |
| F4 fallback parser blind to strings and quoted names | `_strip_sql`: comments removed, string literals and quoted identifiers (`'…'`, `"…"`, `` `…` ``, `[…]`) emptied | `test_the_fallback_without_rowid_parser_on_its_own` (11 statements, compared with SQLite's own `wr`) |
| F5 STORED generated columns dropped | `table_xinfo` hidden 0 or 3 read; VIRTUAL (2) still excluded | `test_a_stored_generated_column_is_read` |
| F6 guards for `_q` and the fallback | — | `test_a_quote_inside_a_name_is_doubled`, the fallback test above |
| F7 an FTS5 table not named `*_fts` has its text counted twice through `<name>_content` | not fixed here: recorded below | — |

**Planted reverts, cycle 5.** All on disk, with `-B` and an empty `PYTHONPYCACHEPREFIX`. Each of
these 11 new reverts was red:

| Revert | Red |
|---|---|
| F1 schema error message kept (store handler) | 1/1 |
| F1 open error message kept | 1/1 |
| F1/F6 catch-all message kept | 1/1 |
| F3 catch-all drops earlier sightings | 1/1 |
| F3 a stopped store says "would not open" | 1/1 |
| F2 a column named `rowid` shadows the rowid | 1/1 |
| F2 all three aliases taken, not read whole | 1/1 |
| F2 `_mod` not type-safe | 1/1 (error) |
| F4/F6 fallback parser ignores strings and names | 3/3 subtests |
| F5 STORED generated columns dropped | 1/1 |
| F6 `_q` does not double a quote | 1/1 |

The 41 earlier reverts were re-targeted at this code, and all were red. That makes 52 of 52; the
46 guarding tests were green before and after, and both sources ended byte-identical.

## Review round 6 → fix cycle 6

The tests were written first. Before the change, 5 of the 6 new tests were red: 7 failures and 1
error, because the path test runs one subtest per name, and the path with a space already worked.
Also red: the extended stopped-store test (L5) and the fallback-parser test with its three new
statements. The two shadowed-rowid tests and the new parser statements were already green:
they guard rules that had no test, and their reverts turn red below.

| Item | Fix | Test (`TranscriptHomes`) |
|---|---|---|
| N1 a store path holding `?` or `#` was reported clean and the scanner created a file; `%XX` was decoded into another path | `paths.sqlite_uri` (`Path.resolve().as_uri()` + `?mode=…`) at every connect site in the engine: the leak scan, `agents_view`, `scan_sessions`, `build_dashboard`, `mcp/server`, `observatory`, `disk_usage`, `build_findings`, `check_store`, `export_ledger`, `review`, `backup_store`, `scrub_companion` (read-only and read-write); `agent_secret_findings` inline. `trace_opens` now decodes such URIs (`urllib.parse`) | `test_a_store_path_with_uri_characters_is_read_read_only` (`?`, `#`, `%41`, a space, `%2F`: the right file, read-only, nothing created, the sighting reported), `test_no_sqlite_uri_is_built_from_a_raw_path` (a source scan of the engine), `test_sqlite_uri_keeps_the_path_whole_and_read_only`; `test_trace_opens.py` checks a percent-encoded URI |
| L1 seven reverts that stayed green | — | `test_a_shadowed_rowid_newest_row_rewritten_is_read`; `test_a_shadowed_rowid_at_max_set_is_read_once_and_edits_are_seen` (sets of 5 and 100); the fallback test gains `[it's]`, `` `it's` `` and `-- it's` before `WITHOUT ROWID` |
| L2 a text mark in an old state file blinded the store | a mark that is not an integer is no mark: the table is read from the start | `test_a_text_mark_from_an_old_state_file_is_read_from_the_start` |
| L3 a table taking all three rowid names | described in ONBOARDING and the Behavior section above: read whole, re-reported every pass | — |
| L4 FTS5 counted twice | in ONBOARDING's Not covered | — |
| L5 a stopped store said `rows_read: 0` | `scan_sqlite(…, tally=…)` counts rows for the caller | `test_a_store_that_stops_midway_keeps_its_sightings_and_says_so` |

**An equivalent mutant.** The verifier's revert of `_strip_sql`'s doubled-quote handling (`''`
inside a literal) stays green on every input. Splitting `'it''s'` into two literals `'it'` and
`'s'` empties the same characters: no `)`, comment or keyword can appear between them. No test
can tell the two apart, so the revert is equivalent, and none was added.

**Tests outside this file that the URI change touched.** These failed on this branch:

- `test_ledger_export.py` read the source for the literal `mode=ro`. It now also accepts
  `paths.sqlite_uri(paths.DB)`, whose default is read-only; any other mode is spelled in the
  call.
- `tests/session_fixture.py` swaps `scan_sessions.paths` for a namespace, which now carries the
  real `sqlite_uri`.

Each module the URI change touched had its suites run, one at a time:

| Suite | Result |
|---|---|
| `test_backup_store` | 28 checks |
| `test_store_integrity` | 51 checks |
| `test_scrub` | 23 checks |
| `test_agent_secrets` | 5 OK |
| `test_agents_page` | 16 OK |
| `test_dashboard_store` | 38 checks |
| `test_plugins` | 45 checks |
| `test_store_faults` | 32 checks |
| `test_schema_compatibility` | 21 OK |
| `test_honest_absence` | 49 checks |
| `test_cause_and_symptom` | 27 checks |
| `test_at_stake` | 18 checks |
| `test_ack` | 26 checks |
| `test_mcp_wire` | 128 checks |
| `test_gate_purity` | 53 checks |
| `test_ledger_export` | 14 checks |

Three suites fail in this harness on the unmodified base (`dbb9617f`) too, identically: their
fixtures want a seeded estate or a workspace this harness does not have.

- `test_trace_opens`: two checks, and its new check passes.
- `test_sessions`: one check; 48 pass.
- `test_review_queue`: six checks.

**Planted reverts, cycle 6.** All on disk, with `-B` and an empty `PYTHONPYCACHEPREFIX`:

| Revert | Red |
|---|---|
| N1 the scanner's URI from the raw path | 5/5 |
| N1 `sqlite_uri` not percent-encoded | 5/5 |
| N1 Observatory's own store from the raw path (`observatory.py`) | 1/1 |
| L1 `_row_digest` on plain `rowid` | 1/1 |
| L1 at-max signature rows on plain `rowid` | 1/1 |
| L1 `_set_digest` on plain `rowid` | 1/1 |
| L1 `at_old_max` on plain `rowid` | 2/2 subtests |
| L1 parser: `[brackets]` not a quote | 1/1 |
| L1 parser: `` `backticks` `` not a quote | 1/1 |
| L1 parser: `--` comments not stripped | 1/1 |
| L2 a text mark raises | 1/1 |
| L5 a stopped store says 0 rows | 1/1 |

The 52 earlier reverts were re-targeted at this code, and all were red: 64 of 64 in all. The 52
guarding tests were green before and after, and the three sources ended byte-identical.

## Review round 7 → polish cycle

Tests were written first. Before the change, the two F3 tests were red, raising the
simulated `RuntimeError`. The AST scan, its 13 bad and 8 good snippets, and the trailing-slash
test were green already: the tree had no raw URI left. They guard the reverts below.

| Item | Fix | Test (`TranscriptHomes`) |
|---|---|---|
| F3 `Path.resolve()` raised RuntimeError on a symlink loop on Python 3.11/3.12; `disk_usage.last_measured`, `build_findings` and `mcp/server` caught only sqlite3.Error (a regression from cycle 6) | `paths.sqlite_uri` spells the path (`os.path.abspath`) and never looks it up; a loop now fails inside `sqlite3.connect` | `test_sqlite_uri_never_touches_the_filesystem`, `test_a_symlink_loop_store_is_unreadable_not_a_traceback` (with `resolve` raising, as on 3.11) |
| F2 the source scan caught one form of 12 | an AST scan: every `connect(…, uri=True)` in the engine takes `sqlite_uri(…)`, `readonly_uri(…)`, `<path>.as_uri() + …`, or a name the function binds only to those; the CHANGELOG says exactly that | `test_no_sqlite_uri_is_built_from_a_raw_path`, `test_the_uri_scan_catches_every_way_of_building_a_raw_uri` (13 bad, 8 good snippets) |
| F1 "nothing is created beside it" | ONBOARDING: no empty file where the path was cut; a WAL store read-only may still create its `-shm`/`-wal` | — |
| F4 the CHANGELOG said every site uses the helper | `agent_secret_findings` now does too; the CHANGELOG names the modules that build their own `as_uri()` URI and the writer's plain path (`store/db.py`); ONBOARDING the same | — |
| F4 a trailing slash | intended: the URI is the normalised path, as `Path("state.db/")` already is; documented in ONBOARDING | `test_a_trailing_slash_is_dropped_as_pathlib_drops_it` |

**F3 on real interpreters.** A probe pointed `paths.DB` at a real symlink loop and called
`disk_usage.last_measured()`. On Python 3.11.16 and 3.12.14, `Path.resolve()` raised
`RuntimeError` on the loop. With this branch both returned `{}`. The previous head, `a555b55e`,
raised `RuntimeError: Symlink loop` on 3.11.

**Planted reverts, polish.** All on disk, with `-B` and an empty `PYTHONPYCACHEPREFIX`:

| Revert | Red |
|---|---|
| F3 `sqlite_uri` calls `resolve()` again | 2/2 (errors) |
| F2 a `%`-format URI at `build_findings.py:403` | 1/1 |
| F2 a two-line build-then-pass URI at the same site | 1/1 |

The 64 earlier reverts were re-run, and all were red: 67 of 67. The 54 guarding tests were green
before and after, and the sources ended byte-identical.

**Seeded suites** through `tests/run_portable.py --suite X --jobs 1`, each in its own sandbox:

| Suite | Result |
|---|---|
| `trace_opens` | 38 assertions |
| `sessions` | 53 |
| `review_queue` | 33 |
| `plugins` (disk usage) | 45 |
| `footprint` | 29 |
| `finding_rules` | 93 |
| `cause_and_symptom` | 27 |
| `tick_findings` | 7 cases |
| `mcp_wire` | 128 |
| `leak_coverage` | 107 cases |
| `leak_scan_incremental` | 8 |
| `private_sources` | 5 |
| `scrub_incremental` | 6 |
| `use_secret` | 85 assertions |

All PASS with 0 failures. The three suites that failed in cycle 6's bare harness (`trace_opens`,
`sessions`, `review_queue`) pass seeded: those failures came from the harness, as recorded there.

**Follow-up, pre-existing.** `store/compatibility.py` `readonly_uri` also calls `resolve()`, so
it carries the same 3.11/3.12 symlink-loop hazard. It predates this pull request, and the
polish leaves it unchanged.

## Review round 8 → the last LOW finding

**The finding.** `os.path.abspath` collapsed `..` lexically. Take `C/lnk/../store.db` with
`lnk` pointing to `A/B/inner`. The plain-path writer (`store/db.py`) and the old `resolve()` both
reach `A/B/store.db`, but `sqlite_uri` named `C/store.db`, so readers and the writer could name
different files.

**The fix.** `paths.sqlite_uri` is now `Path(path).absolute().as_uri()` plus `?mode=…`:

- it keeps `..` and looks nothing up, so a symlink loop is no error on 3.11.16 or 3.12.14 (the
  probe re-run on both);
- it drops only a trailing slash and `.` parts, as `pathlib` does;
- `as_uri()` still percent-encodes `?`, `#`, `%` and spaces.

The docstring and both ONBOARDING copies say so.

**Tests.** `test_dot_dot_after_a_symlink_names_the_file_a_plain_open_names` was red before the
fix: the reader got "a different file". `test_the_uri_encodes_question_mark_hash_and_percent`
guards the encoding.

**Planted reverts.** The new revert (`os.path.abspath` restored) is red. The earlier F3 and N1
reverts of `sqlite_uri` were re-targeted at the new line. All 68 are red; the 55 guarding tests
were green before and after.

**Suites.** `test_leak_coverage` ran 109 OK. Seeded through `run_portable --jobs 1`:

| Suite | Result |
|---|---|
| `leak_coverage` | 109 cases |
| `trace_opens` | 38 assertions |
| `plugins` | 45 assertions |
| `osprivacy` | 17 cases |

**Recorded only (LOW): the F2 AST scan's known blind spots.** None of these is in the tree
today:

- `functools.partial(sqlite3.connect, …)`;
- `uri` passed through `**kwargs` or positionally;
- a `sqlite3.Connection(…)` constructor;
- `<path>.as_uri() + <raw text>` that rebuilds a query from unencoded parts;
- a URI name extended with `+=`.

`store/compatibility.py:30` (`readonly_uri`) still calls `resolve()`, with the same 3.11/3.12
symlink-loop hazard. It predates this pull request.

## Checks run

**The first commit.** The tests were written first. `TranscriptHomes` then had 14 tests: run
before the change, they gave 13 failures and 3 errors (16 outcomes, because one test runs three
subtests). Among them, `test_a_value_planted_in_a_codex_rollout_is_a_sighting` found no hit, and
`not_scanned` held only the tracked-tree note. A fifteenth test, a forced missing zstd decoder,
was added before that commit.

**Test counts by cycle:**

| Cycle | New tests | `TranscriptHomes` | Module |
|---|---|---|---|
| Fix cycle 1 | 14 | 29 | 65 |
| Fix cycle 2 | 8 | 37 | 73 |
| Fix cycle 3 | 8 | 45 | 81 |
| Fix cycle 4 | 7 | 52 | 88 |
| Fix cycle 5 | 9 | 61 | 97 |
| Fix cycle 6 | 6 | 67 | 103 |
| Polish | 4 | 71 | 107 |

The suites in fix cycle 6 ran one at a time. HOME, `OBSERVATORY_HOME` and `TMPDIR` were
synthetic, and every transcript-home variable was unset:

| Suite | Result |
|---|---|
| `test_leak_coverage` | 103 OK |
| `test_leak_scan_incremental` | 8 OK |
| `test_use_secret.py` (the whole script) | 85 checks pass, none fail or skip |
| `test_private_sources` | 5 OK |
| `test_scrub_incremental` | 6 OK |

Then `tools/update_inventory.py` followed by `--check`, `tools/sync_engine_docs.py --check`, and
`tools/check_public_release.py`; the results are in the PR comment.

**Test isolation.** `test_leak_coverage` and `test_leak_scan_incremental` set HOME to a synthetic
folder and unset every override. In the first commit, `test_use_secret.py` only pointed the
Codex, Hermes and opencode overrides at absent folders and left HOME as it was. Since fix cycle
1, its scanner subprocesses get a synthetic HOME (and USERPROFILE) with no override. Its
workspace stays in the `tests/tmp.py` sandbox.

Not run: `test_finding_rules.py`, which needs an initialized workspace and does not import the
scanner; the full suite and the OS matrix, which nightly CI runs.

## Not covered

**Seen only by the weekly complete pass (`tools/sqlite_scan.py`):**

- A row replaced under its own rowid below the newest row, in a table with no update column.
  This covers `DELETE id=2` + `INSERT id=2`, and `INSERT OR REPLACE`.
- An edit that moves no column. Codex runs `UPDATE threads SET title = ? WHERE id = ?`.
- An edit below a maximum that is already in the future.
- Codex `thread_items`. It is upserted in place (`ON CONFLICT … DO UPDATE SET item_json …`), and
  its `updated_at_ordinal` counts per thread. The rollout `.jsonl` files carry the same content
  and are read incrementally.
- An edit that keeps a BLOB update column's value unchanged.
- An update column holding mixed types. Its maximum is compared within one type only.
- A change of rowids under the mark whose count, rowid sum and sum of squares all collide.
  Impractical with two independent primes, not impossible.

**Follow-ups, out of scope here (L6, round 3; these predate I-5):**

- A row with rowid ≤ 0 is never read: every read starts above rowid 0.
- A WITHOUT ROWID table is read whole on every pass, so a value in one is reported every pass.
- Text kept in a column without text affinity (`INTEGER`, `REAL`) is not read.
- An FTS5 table whose name does not match `*_fts` (F7, round 5) has its text counted twice: once
  in the table itself and once through its `<name>_content` shadow table. The name-based skip of
  FTS mirrors would better be a check of the module in `sqlite_master.sql`.
- A table that can never be read keeps its store's marks still (L4, round 4). Every pass over
  that store re-reads from the old marks, and is complete if it never had any.
- Done in fix cycle 3 instead, because it was one line: `VARCHAR(n)` and `NCHAR(n)` columns were
  skipped.

**Not read at all:**

- Codex's `sqlite_home` in `config.toml`; only `CODEX_SQLITE_HOME` is read.
- Codex development databases under `~/.codex/sqlite/*.db`, which the installed binary does not
  name.
- Transcripts of other CLIs: Cursor, Gemini CLI, Kiro.

**Not tested on:** Windows. The default paths follow each tool's own rules, but no Windows run
was made.
