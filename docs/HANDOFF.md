# Full-system release handoff

Objective: distribute the complete Project Observatory engine as open source while
each user retains their own private projects, credentials and runtime state; retain
older CLI contracts, provide future upgrade/restore safeguards, and publish accurate
onboarding, website and launch material.

## Completed implementation

- Complete source export under `observatory/engine/`; generic defaults, individual
  integration/feature opt-ins, explicit per-user source paths and private workspace.
- Original portable CLI preserved. Full engine enters through `full`; homes and
  formats remain distinct. Versioned config/workspace/registry/database/plugin/tool
  contracts, supported migration checks, backup/apply/restore and unknown-version
  refusal are documented in [COMPATIBILITY.md](COMPATIBILITY.md).
- Full engine source and installed-wheel synthetic checks completed; receipt in
  [RELEASE.md](RELEASE.md). CI repeats on Linux 3.11/3.14 and macOS 3.14.
- Credential/keyserver/provider failure fixes and portable dashboard commands.
- Public site describes the full release; canonical [article](../site/field-notes/index.html),
  generated cover and [social drafts](content/README.md). Social accounts were not posted to.
- Skills remains the family site's primary entry. Harness is its separate section;
  Observatory observes projects, Asset Foundry is explicitly in development.

## Module and task packets

[Migration/capability map](MIGRATION.md), [agent onboarding](AGENT-ONBOARDING.md),
[product scenarios](ux/scenarios.md), [next UX packets](ux/UI-PLAN.md),
[site scenarios](site/BRIEF.md), [deployment procedure](site/DEPLOY.md),
[case-study boundaries](site/CASE-STUDY.md), [security policy](../SECURITY.md).

## Local-only rule

Never add a real workspace, database, private denylist, registry, credentials,
operational receipt, provider response or local machine configuration to this
repository. Never deploy generated private dashboard pages. The public source has
no private operational ancestry. Original installed services were not switched.

## Published release and deployment

- [0.2.0 release](https://github.com/passioncode-ai/project-observatory-dashboard/releases/tag/v0.2.0): source `fb8d692416da63323d29ae4f89ab71f5c5ba3faa`, inspected wheel and SHA256SUMS. Downloaded release assets match the reviewed archive.
- [Engine CI](https://github.com/ssheleg/project-observatory-open-source/actions/runs/35599940689): Linux Python 3.11/3.14 and macOS Python 3.14 all pass.
- [Website compatibility correction](https://github.com/ssheleg/project-observatory-open-source/commit/c773b5d483a75e7c79fce520a97d4a85c1d6a300): content-versioned CSS/JS prevents previous browser caches breaking new pages; eight static negative probes pass. [CI](https://github.com/ssheleg/project-observatory-open-source/actions/runs/35600519090) passes all platforms.
- [Production](https://observatory.sshlg.me/) and [article](https://observatory.sshlg.me/field-notes/): deployed website commit above, Cloudflare deployment `6a319bfa-aa59-48a3-ad31-4843a1809c64`. Ten served files match source bytes; the eleventh file supplies verified response headers.
- Both pages checked at 320px and 1440px with no overflow, including the previously cached browser. Copy and before/after actions work; no console warnings/errors.

[Machine receipt](releases/0.2.0.json) separates package source, website source,
CI, asset digests and deployment. The website-only fix did not change the wheel.
This follow-up changes documentation only and does not imply another deployment.

## Next task and prerequisites

**Status 2026-09-28, newest first:** the always-on server speaks `fabric-service/0.1` (instance
lock, well-known document, token-guarded events feed, descriptor from the installer) on branch
`agent/fabric-service`, unreleased. Entry point and the next task:
[runs/2026-09-28-fabric-service](runs/2026-09-28-fabric-service/README.md).

**Status 2026-09-26 (evening):** 0.4.0 — English/Russian dashboard, the PassionCode
design system and the move to `passioncode-ai`. Entry point and open work:
[runs/2026-09-26-i18n-passioncode](runs/2026-09-26-i18n-passioncode/README.md).

**Status 2026-09-23 (evening):** 0.2.0 shipped with defects that the private predecessor had
already fixed, one of them a security defect (a local `rotate` marked a leaked credential as closed;
advisory GHSA-x9pc-v2g8-q5jm). 0.2.1 carries every one of them with regression tests, 0.2.2 fixes
`migrate-local` for installations with runtime identities, 0.2.3 gives launchd jobs the installer's
PATH. See [CHANGELOG](../CHANGELOG.md). This repository is the engine's single upstream: changes land
here first, the source inventory is kept current with `tools/update_inventory.py`, and `main` is
protected (three required checks, linear history, no force-push).

Open work, in order: the MCP SDK 2.2 upgrade together with the lock file (Dependabot #24), then the
product paths — relations and agent work reports, credential lifecycle, verified restore. A new
operator starts with [agent onboarding](AGENT-ONBOARDING.md); `project-observatory full open` shows
the dashboard, `project-observatory full agent install` connects Claude Code with plugin
auto-update, and `project-observatory full doctor` names any enabled integration whose source is
missing. An existing installation moves with `full migrate-local` while its writers are stopped;
no implicit switch is made. Preserve the local-only rule above.

## Latest editorial delivery

The Claude Code credential-copy article and channel posts were revised and the
website redeployed. See the [editorial handoff](runs/2026-09-21-credential-copy-editorial.md)
for its source revision, checks and deployment; the package release above is unchanged.

## Latest owner-origin article delivery

The field-notes article now tells the owner's path from project inventory and
revival to repeated key warnings and monitoring. The
[origin-story handoff](runs/2026-09-21-observatory-origin-story.md) supersedes the
previous article wording and contains its source ledger, validation and production
receipt. The canonical URL is unchanged. Social posts remain unpublished drafts.

## Illustrated reading and SEO update

[Latest bounded handoff](runs/2026-09-21-reading-seo.md) records the cross-site article, reading and search work, checks and delivery status.


## Dashboard website and repository rename

[2026-09-23 dashboard-site handoff](runs/2026-09-23-dashboard-site.md) is the entry
point for the repository rename and prompt-first public website refresh.
