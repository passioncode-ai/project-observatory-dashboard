# Observatory origin story

## Objective and scope

Rewrite the canonical field-notes article as the owner's first-person account,
with relevant biography, the original project-inventory problem, repeated key
warnings and the monitoring discoveries that expanded the product. Publish the
revised website; keep social accounts untouched. Align the X Article draft.

Baseline: [41f53db](https://github.com/ssheleg/project-observatory-open-source/commit/41f53dbc99d2b3d574f899a89cf69ce8ae274dc1).
Owner explicitly authorized autonomous revision and deployment. No extra intake
was needed. Runtime, package release and private installation are outside this change.

## Route and implementation

Toolbox measurement: `npx sshlg-skills toolkit --for` with the origin-story brief
reported 586 callable skills, 35 in the family. Route was printed before writing.
Read skill sources: copywriting/SKILL.md and brand-voice/SKILL.md in the installed
Agent Skills catalog; task-pipeline/SKILL.md from plugin 1.87.0. Reused the existing
Cloudflare/Wrangler deployment procedure. Brand pack, site scenarios, migration
capability map and reviewed case study were read before drafting.

- copywriting: first-person article, title, preview and metadata; own editorial pass.
- brand-voice: sourced biography and owner-origin facts, revised narrative direction.
- task-pipeline: bounded change, checks, source delivery and publication handoff.
- Cloudflare/Wrangler: exact-source static deployment and production verification.

The article has 1508 whitespace-delimited words. Its arc is inventory and revival,
repeated warnings, observed local copies, mechanism, external context, the broader
product and agent-led setup. Existing visual components and tokens are unchanged.
Headline alternatives: a purely revival-led headline hides the credential discovery;
the previous Claude Code headline hides the owner. The selected title carries both.

Humanization: on, own pass. The review varied cadence, removed a vague sentence
about accumulating observations, and tightened the transition into key management.
[Measured draft-to-final edits](2026-09-21-origin-story-editorial-review.json):
55 changed word spans / 1508 words (3.65%). This compares the new draft with its
reviewed text, not the deliberately replaced previous article. No automated
human-authorship score or universal quality claim is made.

## Source ledger

All sources checked 2026-09-21. Each source establishes only its own category.

| Claim | Receipt and scope |
|---|---|
| Owner's origin and motivation | Owner's task instruction on 2026-09-21: map accumulated folders to repositories, audit what exists/missing, identify projects worth reviving; repeated key-exposure notifications became frustrating; monitoring made the copies visible and expanded the project. First-person testimony, not independently measured chronology. No invented dates, counts of warnings or private project names. |
| Public biography | [Sergey Sheleg's public profile](https://sshlg.me/): tech founder, 13 years building products, startup co-founder, former Head of Android for the Ultimate Guitar app. Public self-description; no audience/install metrics added. |
| Local cleanup aggregate | [Case study at baseline](https://github.com/ssheleg/project-observatory-open-source/blob/41f53dbc99d2b3d574f899a89cf69ce8ae274dc1/docs/site/CASE-STUDY.md) and [machine aggregate](../site/case-study.json): 66 + 136 = 202 value-replacement events. Neither unique-key count nor external compromise. Private journal not published. |
| Possible tool-output retention route | [Claude Code hooks](https://code.claude.com/docs/en/hooks) and [claude-mem hooks](https://docs.claude-mem.ai/architecture/hooks). Capability documentation, not forensic attribution of each historical copy. |
| Old conversations after better storage | [Reddit original post](https://www.reddit.com/r/ClaudeCode/comments/1tjj77i/what_is_a_good_way_to_clean_up_keyssecrets_from/). Author's self-report; no prevalence estimate or unsupported comment reused. |
| Separate attacker-driven scenario | [Check Point primary research](https://research.checkpoint.com/2026/rce-and-api-token-exfiltration-through-claude-code-project-files-cve-2025-59536/), by Aviv Donenfeld and Oded Vanunu. Report explicitly states all reported issues patched before publication. The article retains that qualifier and distinguishes this from local retention. |
| Video attribution | The primary Check Point report links its API-key exfiltration demonstration to [this video](https://www.youtube.com/watch?v=jMeeVxqU3hY). The destination was verified from the report; standalone video fetching was throttled, so no claim of watching/verifying its contents is made. No external media is embedded. |
| Shipped capabilities and limits | [Migration/capability map](../MIGRATION.md), [security policy](../../SECURITY.md), [agent onboarding](../AGENT-ONBOARDING.md). No new runtime feature or universal scanning promise. |

No suitable primary X post was required to make the narrative work. The article
uses a directly relevant Reddit account and primary research with a video instead
of padding the reference list with loosely related social posts.

## Verification and publication

Observed before source integration:

- `python3 docs/site/check.py --self-test`: 11 static files and eight negative probes pass.
- `python3 docs/brand/lint.py --brief`: zero errors, 310 inherited whole-engine warnings.
- `git diff --check`: pass.
- Markdown/rendered-HTML text parity, X Article alignment, single h1 and Article
  schema/headline parity: pass.
- Browser: article at 1440px and 320px; cover loads, heading readable, document
  width equals viewport width. Updated landing story card verified at 320px.
  No article console warnings/errors observed.

The public release gate with the local-only private denylist passed: 302 files,
389 historical blobs, 302 private identifiers, zero findings. It checks patterns
and identifiers, not an absolute absence of every possible private fact.

Completed. Source [8fd0858](https://github.com/ssheleg/project-observatory-open-source/commit/8fd0858af86ec8d5e5015e9eb5d0d481edc2e866)
merged through [PR6](https://github.com/ssheleg/project-observatory-open-source/pull/6)
after all Linux Python 3.11/3.14 and macOS Python 3.14 jobs passed in
[push CI](https://github.com/ssheleg/project-observatory-open-source/actions/runs/35606152890)
and [PR CI](https://github.com/ssheleg/project-observatory-open-source/actions/runs/35606194423).

Cloudflare deployment `89e79cbe-f9e4-4f12-8fbe-25258d778711` serves that source at
the [canonical article](https://observatory.sshlg.me/field-notes/). Ten served asset
bodies match source SHA-256 digests; response security headers match the static
policy. Browser production check: new title and author, cover loaded, no overflow
at 390px, no console warnings/errors. [Machine receipt](2026-09-21-origin-story-production.json).

A fresh checkout of source branch commit `a2061fc4972b0643cb803b98bd88d0107b341bac`
passed the static gate and resolved 24 repository-relative handoff/packet links.
The merged source has the identical tree. This documentation-only receipt requires
no second deployment and does not alter the package release.

## Context, local-only rule and next task

Canonical text: [long-post.md](../content/long-post.md). Rendered page:
[field-notes/index.html](../../site/field-notes/index.html). The [publication packet](../content/README.md)
links channel drafts; [site scenarios](../site/BRIEF.md) define the reader path.
Brand facts and case-study scope constrain subsequent edits.

Never add private registries, keys, raw transcripts, incident journals, machine
configuration, or the local privacy denylist to Git or the static deployment.
Preserve the generated cover's reviewed bytes. Future first-person anecdotes need
owner testimony; future measured claims need a separate reviewed receipt.

Next task after delivery: use the canonical article as the destination for the
existing social drafts. Posting to accounts remains a separate operator action.
