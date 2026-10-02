# Full-system release handoff

Objective: distribute the complete Project Observatory engine as public source code (MIT up to
v0.8.1, PolyForm Noncommercial or Internal Use for v0.8.2–v0.9.1, and `AGPL-3.0-only OR
LicenseRef-PassionCode-Commercial` from then on) while
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
  Observatory observes projects.

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

- Website, 2026-09-29: source `dc0c00b` (#78, the landing no longer names a personal agent), Cloudflare Pages production deployment `d0c9dbe0-32e4-449b-922a-801502181efd`. Both hosts served bytes identical to `site/index.html` after propagation; an unknown path returns 404; CSP and frame headers present.
- [0.2.0 release](https://github.com/passioncode-ai/project-observatory-dashboard/releases/tag/v0.2.0): source `fb8d692416da63323d29ae4f89ab71f5c5ba3faa`, inspected wheel and SHA256SUMS. Downloaded release assets match the reviewed archive.
- [Engine CI](https://github.com/ssheleg/project-observatory-open-source/actions/runs/35599940689): Linux Python 3.11/3.14 and macOS Python 3.14 all pass.
- [Website compatibility correction](https://github.com/ssheleg/project-observatory-open-source/commit/c773b5d483a75e7c79fce520a97d4a85c1d6a300): content-versioned CSS/JS prevents previous browser caches breaking new pages; eight static negative probes pass. [CI](https://github.com/ssheleg/project-observatory-open-source/actions/runs/35600519090) passes all platforms.
- [Production](https://observatory.sshlg.me/) and [article](https://observatory.sshlg.me/field-notes/): deployed website commit above, Cloudflare deployment `6a319bfa-aa59-48a3-ad31-4843a1809c64`. Ten served files match source bytes; the eleventh file supplies verified response headers.
- Both pages checked at 320px and 1440px with no overflow, including the previously cached browser. Copy and before/after actions work; no console warnings/errors.

[Machine receipt](releases/0.2.0.json) separates package source, website source,
CI, asset digests and deployment. The website-only fix did not change the wheel.
This follow-up changes documentation only and does not imply another deployment.

## Next task and prerequisites

**2026-10-01, client disconnect handling (proposed, unreleased):** expected socket
closures no longer produce server tracebacks; unrelated I/O failures remain visible.
Test-first receipt and exact next task:
[runs/2026-10-01-client-disconnect](runs/2026-10-01-client-disconnect/README.md).

**Status 2026-10-02: 0.11.0** — the native Mac app and shared assistant, an agent channel
an agent can read (`observatory_overview`, bounded and paged tools, the survey contract kept),
and the dashboard audit; companion plugin `observatory-log` 0.13.1. Record, REQ table and the
exact next task: [runs/2026-10-02-app-agent-audit](runs/2026-10-02-app-agent-audit/README.md).

**Status 2026-10-01: 0.10.0 released** — the first release under `AGPL-3.0-only OR
LicenseRef-PassionCode-Commercial`, with honest absence, the Cloudflare presets, suppression
identity and the reset-date fix; companion plugin `observatory-log` 0.13.0. One operator machine
updated with `full update --apply` and reports `ready` with no reasons. Next task: re-collect or
reinterpret receipts an older release wrote, right after an update. Record:
[runs/2026-10-01-release-0.10.0](runs/2026-10-01-release-0.10.0/README.md).

**Status 2026-10-01, honest absence (released in 0.10.0):** a source measured as not
applicable here (no RDAP service for the TLD, a companion tool not installed, a credential not
set up for a surface another reads, a bounded OpenRouter listing that missed no consumer, a
receipt of an integration that is off) goes on `not_applicable` and no longer degrades the
service; a one-branch remote resolves its default branch; a timed-out command is asked again.
Record:
[runs/2026-10-01-honest-absence](runs/2026-10-01-honest-absence/README.md).

**Status 2026-10-01, open pull requests:** #83, #89, #93 and #62 landed with fixes, plus #98
(reset dates counted from the measurement, which had turned `main` red). Follow-ups #95–#97.
Record: [runs/2026-10-01-open-pr-sweep](runs/2026-10-01-open-pr-sweep/README.md).

**Status 2026-10-01, agent coordination:** shared files (`CHANGELOG.md`, this file) are edited
under an agent-sync lease decided by git refs on `origin`; [AGENT_SYNC.md](AGENT_SYNC.md) is the
generated wiring. Record: [runs/2026-10-01-agent-sync](runs/2026-10-01-agent-sync/README.md).

**Status 2026-10-01, leak suppression identity (released in 0.10.0):** a suppression now
binds to the sighting's exact location and a workspace-keyed `version_id`, so it cannot hide a
rotated value or another file; expired or changed rules replay old evidence. `CHANGELOG.md`
0.10.0 describes it. Entry point:
[runs/2026-09-25-suppression-boundaries](runs/2026-09-25-suppression-boundaries/README.md).

**Status 2026-09-30, licence and repository standard (released in 0.10.0):** the code is
`AGPL-3.0-only OR LicenseRef-PassionCode-Commercial` from 0.10.0; v0.9.1 and earlier
keep PolyForm or MIT. README and AGENTS.md follow the organisation's repository standard. Entry
point and the next task: [runs/2026-09-30-standard-agpl](runs/2026-09-30-standard-agpl/README.md).

**Status 2026-09-30:** 0.9.1 — `machine.mcp.inventory` (every MCP server the agent
configs declare, by name) and `fabric-interop/0.1` on the MCP server (capability tools, trace
context, jobs). Entry point and the next task:
[runs/2026-09-30-mcp-inventory-interop](runs/2026-09-30-mcp-inventory-interop/README.md).

**Status 2026-09-28:** the always-on server speaks `fabric-service/0.1` (instance
lock, well-known document, token-guarded events feed, descriptor from the installer) on branch
`agent/fabric-service`, unreleased. Entry point and the next task:
[runs/2026-09-28-fabric-service](runs/2026-09-28-fabric-service/README.md).

**Current status, 2026-09-28: 0.6.3.** The package installs the short name `observatory`
beside `project-observatory`, and either name with no arguments opens the dashboard of the
workspace in `OBSERVATORY_HOME` ([CHANGELOG](../CHANGELOG.md#063--2026-09-28)). 0.6.3 has no
run receipt of its own; the receipts of the releases before it are the entry points:

| Receipt | Releases | What it covers |
|---|---|---|
| [runs/2026-09-27-machine](runs/2026-09-27-machine/README.md) | 0.6.0–0.6.2 | machine survey (processes by origin, memory, disk, swap), git hygiene, `full cleanup`, the Machine page |
| [runs/2026-09-27-backups-and-organizations](runs/2026-09-27-backups-and-organizations/README.md) | 0.4.1, 0.5.0 | encrypted backups off the disk; organizations and resources; `observatory-log` 0.12.0 |

Contributors work from [AGENTS.md](../AGENTS.md): what this repository is, the exact checks,
the privacy rules and the merge flow.

**Next tasks for a contributor** (this repository and synthetic fixtures only):

1. **The `analytics.stale` remedy names a command the step runner refuses**
   (`./observatory.py google --force` answers "step arguments are not accepted here"). Fix the
   remedy text or let the step take `--force`, with a test that runs the printed remedy.
   Open since the 0.5.0 receipt.
2. `test_schema_compatibility.test_many_concurrent_first_opens` failed once under heavy memory
   pressure and passed on re-run: give it a load-independent assertion or a longer timeout.
3. `project-observatory full update`, the command the release cycle in AGENTS.md ends with, is
   being built in its own change; until it lands, a machine installs the new wheel into its
   environment and runs `full upgrade` (a preview; `--apply --writers-stopped` applies it).

**Next tasks for the operator only** (they need the operator's machine, accounts or decision;
no contributor can do them):

1. Free disk on the operator's machine: the machine receipt names the VM disk, swap and
   simulators as the causes. The observatory reports and never stops a process.
2. Keep the backup passphrase outside the machine (`full backup-passphrase show` in a terminal).
3. Configure the GA4 account and Figma team of the one organization that has neither.
4. Decide whether `features.companion_remediation` stays on while the companion's database is
   absent.
5. Review the idle unique branches: `full cleanup --apply --include manual` bundles them first.

**Status 2026-09-26 (evening), history below, newest first:** 0.4.0 — English/Russian dashboard, the PassionCode
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
