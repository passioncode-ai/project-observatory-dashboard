# Run 2026-11-11: release 0.35.13

Objective: three requests from the Fabric board, taken right after 0.35.12 so its CI round was not restarted: a
Cloudflare preset for one zone's settings, the vault read that served an older copy, and Docker images at critical.

## State now

- Open: none for the release; the maintenance job installs 0.35.13 on the other machines.
- Waits: none.
- Next task: OBS-504 (the tick's dashboard step cut at its time limit), then OBS-506, OBS-505, OBS-507 and OBS-508.

## What it releases

- OBS-509: `cloudflare.py issue --preset zone-settings --zone ZONE` (Zone Read, Zone Settings Write on one zone,
  proved by reading its settings), offered to credential requests; asked for by a site agent to switch off a zone's
  Google tag gateway.
- OBS-510: `use_secret.py run` reads the project's own folder first, where `vault.py put` writes, so a key put again
  by an old name is the one served; the older copy is named on stderr. Reported by a peer agent.
- OBS-511: the disk guard runs `docker image prune -af --filter until=168h` at critical while a local daemon already
  runs; the guard section 0.35.12 shipped is recorded in `defaults/cleanup-history.json`.

## Checks run

Listed in the release pull request: the full portable set on this tree, the root suite and the gates.
