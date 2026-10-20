# Run 2026-10-20: release 0.26.0

Objective (operator request, 2026-10-20): ship the Stop hook fix (OBS-169, #315) to the operator's machine,
which needs an engine release and an `observatory-log` plugin release.

Base: `b8181ead` (origin/main, #315 merged) on branch `release/0.26.0`. This record is committed with the
version bump; resolve the final object id with `git log -1 --format=%H` on that branch.

## What the release carries

Everything merged since `v0.25.0`: OBS-162 (delegation verifier, DEC-0045), OBS-156 (Heroku door, Cloudflare
team side), OBS-157 (machine backup and restore), OBS-159 (session MCP servers move onto the installed release),
OBS-160 (Fabric project id), OBS-164 to OBS-168 (backlog rows only) and OBS-169 (#315). The rows of OBS-156,
OBS-157, OBS-159, OBS-160 and OBS-162 stay with their owners to close with their outcomes.

## Decisions

- **0.26.0, not a patch**: the line adds features (semantic versioning, minor). The release iteration's plan
  named its next release 0.26.0 too; it becomes the following version.
- **The plugin moves with the engine** to 0.26.0 (three manifests and every `SKILL.md`), because #315 changed
  its hooks (`record-turn.sh`, `hooks.json`).
- **No `RELEASE.json` marker**: nothing in this line needs a person before it is installed.

## Steps and evidence

Filled in as each step completes: the pull request and its checks, the merge commit, the annotated tag, the
release run, the verified assets, the PassionCode set re-pin and the installed versions.
