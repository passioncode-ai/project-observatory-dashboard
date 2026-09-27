---
name: tracking-resources
description: >-
  Use whenever an agent creates, connects or moves anything outside the repository
  for a watched project — a Google Analytics property or other analytics tracker,
  a Firebase or Google Cloud project, a server, database, DNS zone, cloud, payment
  or app-store account, a Figma file: "add analytics", "create a Firebase project",
  "set up a server", "new Figma file", «подключи аналитику», «создай Firebase»,
  «подними сервер», «новый файл в Figma». Reads whose accounts the project uses
  before creating, and records what was created through Project Observatory in the
  same turn. NOT for secrets (handling-secrets) or for code changes inside the repository.
license: MIT
metadata:
  version: "0.12.0"
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
                          "account": "accounts/162941847",
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
