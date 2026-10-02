# Project Observatory for macOS

A native window onto the workspace's **dashboard** — the app opens on it — with a
shared advisory assistant one window away (⇧⌘A). The app needs an engine that serves
`observatory-assistant/1` with the `dashboard`/`serve`/`build` actions — **0.12.0 or
newer**; an older release is reported as an incompatible engine with the update
command.

- **Live** when the workspace's own server answers on 127.0.0.1 (it is verified to
  serve this workspace, not just any server on the port).
- **Saved pages** when it does not: the built pages in `docs/dashboard/` are
  self-contained, so reading never waits; a banner names their build time and offers
  **Start server**. An installed always-on server is restarted through its launchd job.
- **Not built yet**: **Build the dashboard** runs the local `dashboard` step.

- [Specification and boundaries](SPEC.md)
- [Requirements and decomposition](PLAN.md)
- [Scenarios](SCENARIOS.md), [flows and screens](FLOWS.md)
- [First verification](../runs/2026-10-01-macos-app/README.md) and the
  [audit and repair that followed](../runs/2026-10-02-app-agent-audit/README.md)

## Build and connect

On macOS 14 or newer with Swift 5.9+ and Python 3.11+:

```sh
swift test --package-path macos
macos/scripts/build-app.sh
```

The bundle is `dist/macos/Project Observatory.app`, with its icon rasterized from the
product mark at every size. `macos/scripts/install-app.sh --open` installs it into
`/Applications` (or `~/Applications`), quits a running copy, forgets Launch Services
registrations of the same bundle left by QA builds elsewhere — Spotlight could open one
of those instead — and opens it. The script signs it ad hoc for local QA. It does not claim a Developer ID, notarization or an App Store release.
Set `OBSERVATORY_SWIFT_BUILD` to reuse a build directory outside the checkout;
`OBSERVATORY_SWIFT_CONFIGURATION=debug` selects the debug build.

Open the app, then Settings. Choose the absolute path to `project-observatory` and
a private initialized workspace outside the source tree. Save and check the
connection. Settings then show the engine version, the protocol and whether the
assistant is ready, or the step that is missing. A typed workspace path is resolved
like a picked one (the engine refuses a path through a symbolic link such as `/tmp`).
A backend must implement `observatory-assistant/1`; 0.10.x does not.
Source QA can use a small executable launcher for `python -m observatory`, with
its working directory set to this checkout and a **synthetic workspace**. Do not
replace a production tagged installation with this branch for testing.

The agent uses the existing configured model/provider and wallet. The app does
not accept API keys. Before sending, it states that the question and selected
local facts go to that provider and can incur model costs. Sources name their
snapshot time; unknown times and missing data remain explicit.

## CLI and MCP

```sh
project-observatory full assistant status
printf '%s' '{"question":"What needs attention?","request_id":"example-request-001"}' |
  project-observatory full assistant ask
```

`ask` returns `conversation_id` and `job`. Use `job` or `cancel` with
`{"id":"job-…"}` on stdin (assistant jobs only); `get` and `delete` accept
`{"id":"chat-…"}`. A missing workspace answers `unknown-workspace`, a full disk
`disk-full`. Repeat the same
request id only with identical input to recover its existing job. Requests are
serialized: a different active question is refused as `assistant-busy`.

The existing MCP server also advertises:

- `observatory_assistant_status`: readiness and private conversation summaries
  (`includeProjects: true` adds the project list a scope picker needs);
- `observatory_assistant_ask`: the same ask contract, local history and model costs;
- `observatory_assistant_conversation`: read a conversation;
- existing `fabric.job.get` and `fabric.job.cancel`: follow/stop the shared job.

These are additive SDK-described MCP tools, not a new published Fabric capability
schema. Existing immutable capability identities and schemas remain unchanged.
The assistant job returns a Fabric result envelope and preserves incoming trace
context. No new MCP server or second registration is needed. Where an installation
uses an MCP gateway, continue using that gateway; do not shadow it in agent config.

`dashboard` verifies that the local server serves the selected workspace before
returning its loopback address. It does not start/rebuild a server or silently
open the default workspace after settings change.

## Persistence and limits

`store/assistant/conversations/` and `store/assistant/requests.json` use private
700/600 directories/files. The runner uses the existing `store/jobs/` store.
Questions are limited to 6,000 characters, a dialogue to 32 turns, the workspace to
100 dialogues and 1,000 tracked request ids. A dialogue leaves only when it is
deleted (File → Delete Conversation, ⌘⌫, or `assistant delete`), with its request
ids and job records; history is never removed to make room. Assistant job records
follow the generic seven-day job retention once their conversation holds the
terminal state, and a request id is kept while its job exists — replaying one after
that starts a new request. The request index keeps input hashes. Do not include
this private state in Git or support attachments.

Model context contains up to seven recent turns and 24,000 characters of
allowlisted evidence, spent in order: this machine's disk and memory (unscoped
questions), then findings most severe first (up to 15, matched to a scoped project
through its repositories, site domains and folder secrets), then projects. Every
source cut short is named in the answer with its count. The workflow has a
300-second deadline, disarmed before the answer is committed. The app runs the CLI
in its own process group, so a timeout or Stop also ends what the CLI started; the
detached job runner has its own session and is not affected. A model answer is
advice and is not written into the canonical registry as an approved fact.
Closing the app leaves an accepted job running. Stop cancels it; provider usage
already incurred can still be charged. Changing settings leaves old jobs in the
original workspace and rejects their late UI callbacks.

## Distribution

Release prerequisites: local gates, native scenario walkthrough, required hosted
checks, normal PR/CLA review, then a tagged compatible engine and app artifact.
For Developer ID signing supply `OBSERVATORY_SIGN_IDENTITY` to the build script.
Notarization is a separate release step using the maintainer's Keychain profile:

```sh
ditto -c -k --keepParent 'dist/macos/Project Observatory.app' dist/Project-Observatory.zip
xcrun notarytool submit dist/Project-Observatory.zip --keychain-profile '<profile>' --wait
xcrun stapler staple 'dist/macos/Project Observatory.app'
codesign --verify --strict 'dist/macos/Project Observatory.app'
spctl --assess --type execute 'dist/macos/Project Observatory.app'
```

These are release instructions, not evidence those operations have run. No signing
credentials, Apple agreements or CLA are supplied or accepted by automation.
