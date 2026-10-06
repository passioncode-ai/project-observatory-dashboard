# 2026-10-05 — releases 0.17.0–0.17.3, the receipt

The 0.17 releases had no receipt (audit A49). This page is that receipt. Every value below can
be read again: from `gh release view vX.Y.Z -R passioncode-ai/project-observatory-dashboard`,
from the `release` workflow run, or from the maintenance job's record on the installed machine.
It was assembled on 2026-10-06.

| Release | Source commit (PR) | Published (UTC) | Wheel SHA-256 (GitHub asset digest) | `release` run |
|---|---|---|---|---|
| [0.17.0](https://github.com/passioncode-ai/project-observatory-dashboard/releases/tag/v0.17.0) | `9bc586eb2fcb` (#169) | 2026-10-05 15:16:32 | `cae8456702b17461ca00d197e41a343caced198033ba01ff2ee80eb4a1cdd342` | 37331123714, success |
| [0.17.1](https://github.com/passioncode-ai/project-observatory-dashboard/releases/tag/v0.17.1) | `fe2be3212e53` (#170) | 2026-10-05 17:05:03 | `4d5b2481b990f607e25f072ae568721563f18112cb2fe85c942009773ee8601c` | 37345122573, success |
| [0.17.2](https://github.com/passioncode-ai/project-observatory-dashboard/releases/tag/v0.17.2) | `6b32053cf08b` (#171) | 2026-10-05 17:58:30 | `e501490166905d2c81ba99493bd690fea9458b9ee2175250a16a8732fec520fe` | 37352145291, success |
| [0.17.3](https://github.com/passioncode-ai/project-observatory-dashboard/releases/tag/v0.17.3) | `58bf0d9aabc4` (#172) | 2026-10-05 18:22:56 | `1c26abb990d9d41051b3fc87f8be10a52350a752c9a00c341a2d18e3cbc278ee` | 37355262270, success |

Each release carries the wheel, the stapled macOS app, `SHA256SUMS` and `SHA256SUMS.asc`.
The CHANGELOG section of each version says what it changed. In short:

- **0.17.0** — updates that install themselves, and data that survives a reinstall.
- **0.17.1** — the daily backup survives a torn database copy.
- **0.17.2** — the maintenance job is no longer throttled; transient refusals are retried hourly; a foreign database is guarded against.
- **0.17.3** — the root cause of the torn copies under concurrent writers is fixed.

## On the maintainer's machine

The maintenance job's record, `store/maintenance.json`, read on 2026-10-06:

- **Engine.** Updated by the job itself from 0.17.2 to 0.17.3 at 2026-10-05 21:19:23 UTC, with exit 0.
  `importlib.metadata` reports `0.17.3` in the engine's environment.
- **Mac app.** Still at 0.17.2. Its swap to 0.17.3 is pending with the result `waiting-for-quit`.
  The app is replaced only while it is not running, so the swap waits for the person to quit it.

The audit that followed these releases, and what it found, is in [2026-10-05-audit](../2026-10-05-audit/README.md).
