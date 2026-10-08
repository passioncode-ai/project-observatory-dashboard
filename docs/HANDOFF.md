# Full-system release handoff

Objective: distribute the complete Project Observatory engine as public source code (MIT up to
v0.8.1, PolyForm Noncommercial or Internal Use for v0.8.2–v0.9.1, and `AGPL-3.0-only OR
LicenseRef-PassionCode-Commercial` from then on) while
each user retains their own private projects, credentials and runtime state; retain
older CLI contracts, provide future upgrade/restore safeguards, and publish accurate
onboarding, website and launch material.

## Current task register and organization quality

Current editable task status lives in [backlog.md](backlog.md), declared by
[backlog-sources.json](backlog-sources.json); dated records below retain their evidence.
The [2026-10-01 quality run](runs/2026-10-01-organization-quality/README.md) connects this
repository to the common workspace backlog and corrects the conformance suite's missing-lsof
expectation. It changes test evidence and documentation, not the published engine version.

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
- Public site described the full release (the site has since been retired; see AGENTS.md); canonical [article](../site/field-notes/index.html),
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

**2026-10-08, release 0.19.3:** four defects a tester found on a workspace with every integration off (OBS-46–49): jobs on the engine's own interpreter (and repaired in place), switched-off sources no longer degrade Health, `lost` follows the sessions switch, suggested products validate. 0.19.2 was tagged and not published; 0.19.3 carries it. Receipt: [runs/2026-10-08-releases-0.19](runs/2026-10-08-releases-0.19/README.md).

**2026-10-08, releases 0.18.0–0.19.2:** signed updates (0.18.0), one update behaviour and a Mac
app that updates itself, in the system language (0.19.0), an update that no longer fails while
agents write (0.19.1), and four defects found on a working machine plus a documentation pass
against the code (0.19.2). Receipt: [runs/2026-10-08-releases-0.19](runs/2026-10-08-releases-0.19/README.md).
Open from this run: the rest of OBS-40 (Health naming incomplete ticks; incremental `google` and
`machine` collectors).

**2026-10-05, releases 0.17.0–0.17.3 (published; the engine installed itself at 0.17.3):**
updates that install themselves, data that survives a reinstall, then three patches from the
live verification. The Mac app's swap to 0.17.3 waits for the app to be quit. Receipt:
[runs/2026-10-05-releases-0.17](runs/2026-10-05-releases-0.17/README.md). The audit of 0.17.3
and its fixes, which become 0.18.0: [runs/2026-10-05-audit](runs/2026-10-05-audit/README.md).

**2026-10-05, release 0.16.0 (published, installed on the maintainer's machine):** agent
memory with access bindings, embedding consent, scoped search with receipts, a loopback HTTP
service and memory/0.1, forgetting with a receipt, and facts that expire, plus the fixes from a
second independent review. The downloaded set was verified, `full update` applied migration 0010,
and the Mac app, `observatory-log` 0.16.0 (through launcher 0.1.28) and the website were updated.
The release gate was approved by an agent on the operator's explicit instruction, which is
recorded as an exception. Record and next task:
[runs/2026-10-05-release-0.16.0](runs/2026-10-05-release-0.16.0/README.md).

**2026-10-04, release 0.15.0 (published, installed on the maintainer's machine):** agent memory
for everyday use (#136) and the header door. Verified downloaded set, `full update` applied
with migration 0009, the Mac app 0.15.0, `observatory-log` 0.15.0 through the PassionCode
launcher 0.1.26/0.1.27, and the website. The release gate was approved by an agent on the
operator's explicit instruction — recorded as the exception it is. Record and next task:
[runs/2026-10-04-release-0.15.0](runs/2026-10-04-release-0.15.0/README.md).

**2026-10-03, release 0.14.0 (published, installed on the maintainer's machine):** the lifecycle
contract, agent memory and its hardening, the first release built, signed, notarized, attested
and published in CI. Receipt — tag, run, digests, verification, installation, launchd state, next
task: [runs/2026-10-03-release-0.14.0](runs/2026-10-03-release-0.14.0/README.md).

**2026-10-03, the product lifecycle contract (branch `claude/lifecycle-contract`, not
released):** the tick no longer runs `claude mcp list`, sizes no privacy-guarded place, bounds
every step and itself, the server idles, every log rotates, a session server on replaced code
answers `stale-server`, plists carry a minimal PATH, builds prune, and a lifecycle watch reports
each product's orphans, stale servers, overruns and oversized logs. Findings fixed and deferred,
tests and the exact next task:
[runs/2026-10-03-lifecycle-contract](runs/2026-10-03-lifecycle-contract/README.md).

**2026-10-03, agent memory for workflows (OBS-02, merged `d6f2b33`, not released):** a checkpoint after every
step, one executor per workflow by lease token, and an immutable handoff pack the engine
assembles, so a workflow continues on another account, model or session when the one that
leaves cannot answer. Five MCP tools, migration `0008-agent-memory-workflows`. Design:
[design/AGENT-MEMORY.md](design/AGENT-MEMORY.md). Record, checks and the exact next task
(OBS-07, then OBS-03): [runs/2026-10-03-agent-memory-workflows](runs/2026-10-03-agent-memory-workflows/README.md).

**2026-10-03, agent memory after the first release (branch `claude/agent-memory-eval`, one pull
request; W1 merged as `b9e5473`, #130):** what each part delivered:
- agents get every credential from Observatory by name, with `--vault-only` and findings for keys outside the vault (W3, OBS-10, OBS-13);
- agents find and recover their work, and the operator gets `full workflow` (W2, OBS-09);
- sessions are linked to workflows (W4, OBS-11);
- the live Agents page (W5, OBS-12);
- the evaluation set, gated (OBS-07);
- search in both languages' word forms, by checkpoint body, with an honest "nothing found" (OBS-03).

Measured: recall@5 0.975 → 1.0; abstention 5 → 20 of 20; checkpoint bodies 0 → 8 of 8. The
redaction on `observatory_record` was quadratic and is now linear. Checks on `687320f` (merged with
0.14.0): 210 of 210 suites. Records under `runs/2026-10-03-agent-*`; numbers in
[reports/2026-10-03-memory-eval-baseline](reports/2026-10-03-memory-eval-baseline/README.md).

**Next:** merge the pull request, then OBS-04: local embeddings chosen on this set, with a
distance floor per model. *(Superseded: merged and released in 0.14.0; what remains of OBS-04 is the operator's decision OBS-35.)*

**Status 2026-10-03: 0.13.0 — three audit-and-fix runs over 0.12.0.** Run 1 (PR #119), run 2
(PR #120) and run 3 (PR #121) each re-walked the build, onboarding, keys, the Keychain, the Mac
app, the scenarios and the docs, and fixed what the one before missed: 73, 27 and 66. Reports:
[run 1](reports/2026-10-03-observatory-audit-run-1/README.md),
[run 2](reports/2026-10-03-observatory-audit-run-2/README.md),
[run 3](reports/2026-10-03-observatory-audit-run-3/README.md). They ship in 0.13.0 with
`observatory-log` 0.14.0; the items only the operator can do are listed at the end of the run-3
report, and `CHANGELOG.md` → 0.13.0 says what changed for a user. The release receipt (tag,
wheel digest, installation, launcher pin, website) is
[runs/2026-10-03-release-0.13.0](runs/2026-10-03-release-0.13.0/README.md).

**2026-10-01, client disconnect handling (released in 0.11.0):** expected socket
closures no longer produce server tracebacks; unrelated I/O failures remain visible.
Test-first receipt and exact next task:
[runs/2026-10-01-client-disconnect](runs/2026-10-01-client-disconnect/README.md).

**Status 2026-10-02 (evening): 0.12.0** — the Mac app opens on the dashboard (live, or the
built pages with Start server), has its icon, always comes back to a window, and can stop what it
starts; finding titles read in Russian. Record and next task:
[runs/2026-10-02-dashboard-first-app](runs/2026-10-02-dashboard-first-app/README.md).

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

**Status 2026-09-28 (history): 0.6.3.** The package installs the short name `observatory`
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

1. *Done in run 3 (2026-10-03):* the `analytics.stale` remedy and six other handed-over
   commands were refused by the step runner; `full google --force` is accepted now, and
   `tests/test_handed_commands.py` parses every `full …` command the engine hands over.
2. `test_schema_compatibility.test_many_concurrent_first_opens` failed once under heavy memory
   pressure and passed on re-run: give it a load-independent assertion or a longer timeout.
3. *Done in 0.7.0:* `project-observatory full update` (a preview; `--apply` installs the
   release, with the tested lock since run 3).

**Next tasks for the operator only** (they need the operator's machine, accounts or decision;
no contributor can do them):

1. Free disk on the operator's machine: the machine receipt names the VM disk, swap and
   simulators as the causes. The observatory reports and never stops a process.
2. Keep the backup passphrase outside the machine (`full backup-passphrase show` in a terminal).
3. Configure the GA4 account and Figma team of the one organization that has neither.
4. Decide whether `features.companion_remediation` stays on while the companion's database is
   absent. *(Settled in 0.19.2: the companion is optional for `sessions`; leave `companion_remediation` off where it is not installed.)*
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
