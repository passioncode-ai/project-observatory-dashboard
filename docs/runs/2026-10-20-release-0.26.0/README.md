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

- **Fix**: #315 squash-merged as `b8181ead` after its required checks passed on the exact head `0d7f7955`
  (main had moved twice; merged into the branch, no force-push).
- **Release PR**: #317 squash-merged as `83ae5682` (exact head `65a71f56`). Its first run failed
  `tests/test_skill_check.py`: the `handling-secrets` handshake command still cited 0.25.0, fixed in the same
  pull request. A macOS job of the push run failed once on a timing assertion in `tests/test_session_reload.py`
  (12.2 s against a 9 s limit) and passed on a rerun: OBS-170.
- **Main CI** on `83ae5682`: run 39382778561 success. Annotated tag `v0.26.0` on `83ae5682`.
- **Release run** [39385168595](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/39385168595):
  guard, wheel, manifest, five desktop builds, feed and publish all success; no reviewer was waited for. Published
  2026-10-20T07:46:28Z, not a prerelease, 19 assets.
- **Verified after publishing**: `shasum -a 256 -c SHA256SUMS` OK on the wheel; `gh attestation verify
  ... --signer-repo passioncode-ai/.github` exit 0; `gpg --verify SHA256SUMS.asc SHA256SUMS` good signature
  (PassionCode.ai releases). The wheel carries `tools/turn_state.py`.
- **PassionCode set**: passioncode #123 (merge `ecebd5e`) pins Observatory Log at `v0.26.0` (it was `v0.18.0`);
  tag `v0.2.16`, release run 39385375052 success, npm `@passioncode-ai/passioncode@0.2.16`; PC-53 closed in #124.
- **Installed on the operator's machine**: `npx --yes @passioncode-ai/passioncode@0.2.16 update` installed
  `observatory-log@passioncode` 0.26.0, whose 9 files are byte-identical to the tag's plugin tree.
  `project-observatory full update --apply --unattended` was refused while a tick held the workspace and
  succeeded on the fourth attempt two minutes apart: `project-observatory --version` 0.26.0, and
  `tools/turn_state.py`, `tools/record_turn.py`, `tools/session_start.py` and `tools/corroborate.py` in the
  installed package match the tag by SHA-256.
- **Live check**: the installed recorder, driven at a checkout holding one untracked folder and four untracked
  files with a fresh session id, answers `recorded: false, reason: nothing changed` (0.16-0.18 s), where 0.25.0
  had recorded "4 files changed, +0/-0" and asked.
- **Knowledge**: fabric-workspace #312 (`4b9ec00`) moves the Observatory and launcher rows.

## Left

- Sessions already running keep the hooks they loaded until they restart; their next turns have no SessionStart
  baseline and take their untracked files as already there.
- The rows of OBS-156, OBS-157, OBS-159, OBS-160 and OBS-162 shipped in 0.26.0 and stay with their owners to close.
