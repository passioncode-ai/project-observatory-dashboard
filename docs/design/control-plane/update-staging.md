# Update staging in proportion to what changes (OBS-626)

## What happened

The maintenance job's automatic update to 0.41.0 on 2026-11-17 stopped the tick and the server at 20:08:00Z and
rolled back at 21:04:47Z: "the new release's workspace upgrade failed (exit 125): TimeoutExpired", the fixed 1800 s
limit of the staged `full upgrade --apply`. The home was 0.87 GB in 1223 files (the store 0.26 GB). The machine was
loaded: the snapshot's copy ran at about 1.4 MB/s, the disk guard reported its free space critical that evening, and a
read two hours later found a load average near 75 and 9.6 GB of swap in use. The same reads on an idle copy of the
home took seconds (hashing 876 MB: 1.6 s; the store's integrity check: 1.8 s).

Read from the workspace's `store/logs/update.jsonl`, the swapped-out copy `*.failed-update-20261117T210447Z-*`,
the snapshot folders and the backups root (times UTC):

| Window | Step | What it did |
|---|---|---|
| 20:08:00 to 20:21:19 | the update's own snapshot (`before-update-*`) | hash the home, copy every file (each read four times by `workspace.copy_private`), hash the copy for the manifest, hash the home again, verify the copy, then `state()` hashed the home once more. 13 min; the copy phase alone 20:09:28 to 20:19:47 |
| 20:21:35 | install | the 0.41.0 wheel |
| 20:21:35 to 20:28:51 | the new release's `upgrade --apply` | hashed the home, took a SECOND whole snapshot (`before-upgrade-*`, manifest 20:26:09), copied that snapshot whole into a stage to merge the settings' defaults and migrate one database, hashed the home again, migrated, and committed: the failed copy's `workspace.json` reads `updated_by 0.41.0` at 20:28:51 |
| 20:28:51 to 20:38:07 | `backup_vault.after_snapshot` inside the upgrade | encrypted and exported its own snapshot (`before-upgrade-20261117T202609Z-*.obsnap`, 434 MB) |
| 20:38:07 to 20:48:56 | `_carry_leftovers` inside the upgrade | encrypted and exported a plaintext `before-upgrade` copy the morning's 0.40.0 attempt had left in `backups/` (`before-upgrade-20261117T051658Z-*.obsnap`) |
| 20:51:35 | the 1800 s limit | killed `python -m observatory`, the launcher only |
| 20:51:56 | the orphaned engine | still running: it removed the exported copies from `backups/` (the folder's mtime) after its launcher was gone |
| 20:59:13, 21:04:47 | undo | rollback wheel installed, the home swapped back from the update's snapshot |

So the upgrade itself took 7 minutes and succeeded; the update rolled back a committed upgrade because the staged
process went on to do backup work the update already does, and the limit ended the launcher but not the engine.

## The change

```
engine_update (the running release)            workspace_upgrade (the new release, a child)
  stop jobs                                        preflight, inventory (hash the home once)
  t0 = clock                                       --snapshot given?
  snapshot -> backups/before-update-X  ------+       yes: verify it, every file that must hold still
  seconds = clock - t0                       |            equals the home now, else refuse naming them
  limit = max(1800, 4 x seconds)             |       no:  take one (clones), under an update's lock
  limit > 4 h?  refuse, nothing installed    |            remove it at the end instead of exporting it
  install wheel                              |     stage: workspace.json, config/, registry/, the store
  upgrade --snapshot X  (limit, group) ------+     migrate the stage, stillness check (cached hashes)
  timeout: kill the whole process group            migrate live, commit
           error names the last step said          each step said on stderr: "Observatory: upgrade step NAME"
  finish: export X encrypted
```

- **One snapshot per update.** `engine_update` passes its own snapshot (`full upgrade --snapshot PATH`); the
  upgrade reuses it as the way back after checking it is one of the home's backups, verifies whole, and holds every
  steady file as the home holds it now. A release that does not know `--snapshot` is asked again without it.
- **Under an update's lock, no export.** An upgrade started by an older update (no `--snapshot`) still takes its own
  snapshot, but while `engine_update.update_lock_held` says an update holds the lock it removes that copy at the
  end (success or a refusal before the commit marker) instead of exporting it or leaving it. A person's
  `full upgrade --apply` keeps the old behaviour: its snapshot is exported or kept.
- **A stage of what changes.** The stage holds `STAGED` (the marker, the settings, the registry a migration may
  read, the store), not the whole home: no copy of the vault, archives or logs.
- **A clone, or one pass.** `_copy_file` clones a file where the volume offers it (APFS `clonefile`, a Linux
  reflink), else streams it once and hashes what it writes; the source is copied again if its status-change time
  moved meanwhile, then refused. Windows keeps `copy_private`.
- **Hashes taken once per process.** `known_digest` reuses a digest while a file's (device, inode, size, mtime,
  ctime) is unchanged and its ctime is older than the hash by `RACY_NS` (2 s); any write moves the ctime, which no
  `utime` sets back. Windows keeps no status-change time and hashes every time.
- **A limit that follows the machine.** `upgrade_limit(seconds) = max(1800, ceil(4 x seconds))`, where `seconds`
  is how long the update's snapshot of this home took just now: the staged upgrade now reads and writes about what
  that snapshot did. Above `UPGRADE_CEILING_S` (4 h) the update refuses before it installs anything
  (`failed_step: upgrade-limit`), names the seconds, megabytes and rate it measured, removes its snapshot and
  starts the jobs again.
- **A limit that ends what it started.** `run_bounded` runs the new release's commands in their own process group
  (a new session on POSIX, a new group on Windows) and ends the whole group on a timeout, a Ctrl-C or any other
  exception, so no engine outlives its launcher. A timeout answers `TimeoutExpired after N s in step NAME`.

## Data

The update's `snapshot` step now records `files`, `bytes`, `seconds` and `upgrade_limit`. A snapshot's result
carries `bytes`. The upgrade's result `backup.kept_by: "update"` says the update keeps the way back.

## Failure behaviour

- A reused snapshot that differs from the home: refused before the stage, nothing changed, the moved paths named
  (at most five, never contents); the update rolls back as for any upgrade failure.
- A staged upgrade still past its limit: the process group is ended, the update rolls back from its snapshot, and
  the error names the step the upgrade had reached.
- The ceiling: refused before the point of change; nothing installed, nothing left in `backups/`.

## Not done here

- `restore_in_place` (the undo) still copies the snapshot back file by file; a clone there would shorten a rollback.
- The space check (`space_writes`) still counts a full copy for the snapshot; with clones it overestimates, the
  safe side. It never counted the second snapshot or the stage the upgrade used to write.
- An update from an engine of 0.41.0 or older still runs that engine's fixed 1800 s limit and kills only the
  launcher; the new release's upgrade under it no longer exports, which is what used the time on 2026-11-17.

## Tests

`observatory/engine/tests/test_workspace_upgrade.py`:

- `ProportionalStaging`: the update's snapshot is reused (no second snapshot); a snapshot that no longer matches is
  refused by name; one outside the home's backups is refused; the stage holds only `STAGED`, never the vault;
  `test_2026_11_17_under_the_updates_lock_the_upgrade_exports_no_snapshot_of_its_own` (red before: the upgrade
  exported its snapshot); a refused upgrade under the lock leaves no copy; each step is named on stderr; a scaled
  home of 40 extra files is hashed at most twice by an upgrade that reuses the snapshot.
- `DigestReuse`: an unchanged file is hashed once; a rewrite keeping size and mtime is still seen; a file changed
  inside the racy window is hashed again.
- `CopyFile`: the streamed copy hashes what it writes; a clone or its fallback gives the same private file with its
  times; a source that keeps changing refuses the copy and leaves nothing.

`observatory/engine/tests/test_engine_update.py`:

- `test_2026_11_17_a_staging_slower_than_the_old_limit_completes`: scaled, the snapshot takes 900 s and the staging
  2400 s (past the old 1800 s); the update completes with a 3600 s limit and passes its snapshot.
- `test_a_limit_that_is_still_exceeded_names_the_step_it_stopped_in`,
  `test_a_staging_the_snapshot_says_cannot_fit_is_refused_before_anything_is_installed`, `test_the_limit_follows_the_snapshot`.
- `RealSeams`: the `--snapshot` argument and the limit reach the child; an older child is asked again without it; a
  timeout names the step; a failure's detail drops the step lines and names the step;
  `test_2026_11_17_a_timeout_ends_the_grandchild_too` (with `subprocess.run` the grandchild wrote its marker; with
  `run_bounded` it does not); `run_bounded` answers like `subprocess.run`.
