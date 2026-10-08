---
name: tracking-resources
description: >-
  Use right after an agent has created, connected or moved something outside the
  repository for a watched project — an analytics property or tracker, a Firebase
  or Google Cloud project, a server, database, DNS zone, cloud, payment or
  app-store account, a Figma file — to record it through Project Observatory in the
  same turn, and just before creating one to read whose account it belongs in.
  Triggers - "record the new resource" / «запиши новый ресурс», "we created a
  Firebase project" / «создали проект Firebase», "log the new server" / «запиши
  новый сервер», "which account should this go in" / «в какой аккаунт это
  создать». NOT for creating the Figma file itself (figma-create-new-file,
  figma-use), wiring analytics or pixels (ad-tracking), provisioning the resource
  with the provider's own tools, secrets (handling-secrets) or code changes inside
  the repository.
license: AGPL-3.0-only OR LicenseRef-PassionCode-Commercial
metadata:
  version: "0.19.3"
compatibility: >-
  Requires a full Project Observatory installation with organizations.json
  configured; the observatory MCP server or its local CLI. Creating the resource
  itself uses the provider's own tools and the user's credentials.
---

# Tracking resources

Everything a project uses outside its repository is recorded in the observatory:
which analytics, which clouds, which servers, which accounts, and under whose
organization. A resource nobody recorded is one the next agent recreates in the
wrong place, and one nobody can find when it breaks or bills.

**This is not optional.** Creating or connecting a resource and not recording it
leaves the task unfinished.

This skill records; it does not create. The resource itself is made with the
tool that owns it — the Figma skills for a design file, ad-tracking for analytics
and pixels, the provider's own CLI or console for the rest — and this skill runs
around that step: before it to pick the account, after it to record the result.

## Before creating anything

1. Identify the project: the SessionStart line names it, or call
   `observatory_project` with its `project:<slug>` id.
2. Read `organization` from that answer. It names the owner and where that
   owner's accounts are: `ga4Account` for Google Analytics, `figmaTeam` and
   `figmaProject` for design files. Create the resource **there**.
3. Stop and ask the operator when:
   - `organization.source` is `conflict` — two owners match and the observatory refuses to guess;
   - the organization is `external` — the code is somebody else's, so create nothing on its behalf;
   - the destination you are told to use differs from the one the observatory names.

## After creating, in the same turn

Report each resource with `observatory_propose`:

```json
{"owner": "agent:<your-name>",
 "targetId": "project:<slug>",
 "patch": {"resources": [{"kind": "ga4-property",
                          "identifier": "properties/123456789",
                          "account": "accounts/123456789",
                          "url": "https://analytics.google.com/…",
                          "note": "web stream for example.com",
                          "added_on": "2026-09-27",
                          "added_by": "agent:<your-name>"}]},
 "evidence": [{"uri": "https://…", "how": "created in the GA admin, stream id …"}]}
```

- `kind` is one of `ga4-property`, `analytics-tracker`, `firebase-project`,
  `gcp-project`, `cloud-account`, `server`, `database`, `dns-zone`, `figma-file`,
  `payment-account`, `app-store`, `other`. Use `other` rather than skip a record.
- `identifier` is the provider's own stable id: a property id, a project id, a
  hostname, a Figma file key. Never a secret value — tokens and keys go through
  handling-secrets.
- Several resources go in one list. Recording is **append-only**: an accepted
  proposal adds to the project's list and never replaces what another agent recorded.
- The proposal is queued for the operator. It lands in the registry when they
  accept it, which is why the evidence has to let them re-check what you saw.

When the observatory is unreachable, record the same facts in the project's own
docs, say so in your reply, and propose them when it is back. Do not drop them.

## Moving or deleting

A resource moved to another account or deleted is reported the same way: send
the same `kind` and `identifier` with the new `account`, or with a `note` that
says it was deleted and when. Later rows update earlier ones.
