# 2026-10-19: release 0.25.0

The public record of one release; the changes are in [CHANGELOG.md](../../../CHANGELOG.md). Release node of
release iteration 1. Cut, published, installed and checked on 2026-10-19.

## State now

0.25.0 is published, verified and installed on a maintainer machine (engine 0.25.0; the desktop app moves on
the maintenance job's next pass). Open from this run: [OBS-154](../../backlog.md) and
[OBS-158](../../backlog.md) (the confirming round's LOW findings). Next task: OBS-158.

## The release

- **Pull request:** #303, which carried the 2026-10-13 recovery work (#260, #278, #280) onto current `main`,
  squash-merged as `ef9c8c33` with `--match-head-commit 25049acd`. Its CI was green on all 10 checks.
- **Rounds:** one adversarial round and two fix cycles closed three MEDIUM findings (a missing worktree whose
  entry could lose its kept HEAD, and the overview and the coverage section claiming full coverage for
  unsurveyed sources); the final confirming round passed with no finding at MEDIUM or above.
- **Tag:** annotated `v0.25.0` on `ef9c8c33`.
- **Release run:** [39254288860](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/39254288860),
  every job green: guard, wheel, manifest, macos, windows x64/arm64, linux x64/arm64, feed, publish.
- **Guard verdict (approval by checks):** pass; `check.yml` was green on the tagged commit
  ([run 39254229801](https://github.com/passioncode-ai/project-observatory-dashboard/actions/runs/39254229801)).
  Nobody clicked.
- **Published** 2026-10-19T13:13:55Z with 19 assets.

## Checks after publishing

- `shasum -a 256 -c SHA256SUMS` on the downloaded wheel: OK.
- `gh attestation verify project_observatory-0.25.0-py3-none-any.whl -R passioncode-ai/project-observatory-dashboard --signer-repo passioncode-ai/.github`:
  exit 0, one SLSA provenance v1 attestation.
- `gpg --verify SHA256SUMS.asc SHA256SUMS`: good signature from the organization's release key.
- `project-observatory full update --apply` on a maintainer machine, then `project-observatory --version`: `0.25.0`.

## Behaviour to know

Disk cleanup removes noticeably less than 0.24.5: a path whose holders could not be checked counts as held and
stays. A worktree whose directory is gone has its Git entry moved aside to `.git/worktrees-retired/` instead of
being removed, so its HEAD stays reachable.
