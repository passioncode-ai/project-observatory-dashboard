# Project Observatory for macOS

A native window onto the workspace's **dashboard** — the app opens on it — with a
shared advisory assistant one window away (⇧⌘A). The app needs an engine that serves
`observatory-assistant/1` with the `dashboard`/`serve`/`build` actions — **0.12.0 or
newer**; an older release is reported as an incompatible engine with the update
command (`project-observatory full update --apply`, which releases before 0.7.0 do not
have: [ONBOARDING → staying in step](../ONBOARDING.md#staying-in-step)).

- **Live** when the workspace's own server answers on 127.0.0.1 (it is verified to
  serve this workspace, not just any server on the port).
- **Saved pages** when it does not: the built pages in `docs/dashboard/` are
  self-contained, so reading never waits; a banner names their build time and offers
  **Start server**, which serves the same pages on 127.0.0.1 (an installed always-on
  server is restarted through its launchd job instead). Key actions on the Keys page are
  commands to copy in both modes; live key actions are `tools/keyserver.py`'s.
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
of those instead — and opens it. `build-app.sh` signs the bundle ad hoc for local QA (with a Developer ID, the hardened runtime and a secure timestamp when `OBSERVATORY_SIGN_IDENTITY` is set) and `install-app.sh` verifies that signature. A release's notarized download is made by `macos/scripts/notarize.sh` ([Signing](#signing)); it is not an App Store release.
Set `OBSERVATORY_SWIFT_BUILD` to reuse a build directory outside the checkout;
`OBSERVATORY_SWIFT_CONFIGURATION=debug` selects the debug build.

On first launch the app looks for the engine at `~/.local/bin/project-observatory`, then
in the virtual environment [README → Install](../../README.md#install) creates
(`~/.local/share/project-observatory-venv/bin/project-observatory`), then in
`/opt/homebrew/bin` and `/usr/local/bin`, and opens the workspace at
`~/.local/share/project-observatory-full`. With no engine there it says «Observatory is
not installed yet» and links the installation guide; a folder that is not a workspace
names `project-observatory full init`; a workspace without pages offers **Build the
dashboard**. Settings (⌘,) takes the absolute path to `project-observatory` and a
private initialized workspace outside the source tree; **Save and check connection**
then shows the engine version, the protocol and whether the assistant is ready, or the
step that is missing. A typed workspace path is resolved like a picked one (the engine
refuses a path through a symbolic link such as `/tmp`). The engine must serve
`observatory-assistant/1` with the dashboard actions: 0.12.0 or newer.

The app is dark in every window and uses the PassionCode colour roles of the dashboard
itself — gold for the primary action, selection and focus — defined once in
`macos/Sources/ObservatoryCore/Palette.swift`; `PaletteTests` holds them to the vendored
token file and to WCAG AA. Nothing in the app reads or writes the macOS Keychain, and the
dashboard view cancels any password or client-certificate challenge rather than letting
WebKit consult the login keychain.
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
checks, the normal pull request (opening it is the CLA agreement — nothing to tick), then a
tagged compatible engine and app artifact.
### Signing

**Releases are signed in CI, not on a laptop.** `.github/workflows/release.yml` runs on a
`vX.Y.Z` tag in the protected `release` environment:
- the organization's CI Developer ID, through `passioncode-ai/.github/actions/apple-signing@v1`;
- notarization and staple, through `actions/notarize@v1`;
- Sigstore attestation, plus `SHA256SUMS` and its GPG signature, through `release-publish.yml@v1`.

A person from `release-approvers` approves the run, and may be whoever pushed the tag; an agent
never approves it. The local path
below is for debugging and for checking the scripts; its output is never attached to a release.


Without `OBSERVATORY_SIGN_IDENTITY` the build script signs ad hoc
(`codesign --sign -`), which touches no keychain. For Developer ID signing supply
`OBSERVATORY_SIGN_IDENTITY` to the build script: `codesign` then signs with the hardened
runtime and a secure timestamp, reading that identity's private key from the login
Keychain (`macos/scripts/build-app.sh`). macOS can ask for the keychain password or for
permission to use the key, unless the key already allows `codesign`. Signing is the
release operator's explicit act, run by hand; nothing in Observatory signs on a schedule.

`macos/scripts/notarize.sh` turns that bundle into the release download:

```sh
OBSERVATORY_SIGN_IDENTITY='Developer ID Application: <name> (<team>)' macos/scripts/build-app.sh
# an App Store Connect API key (no Keychain involved) …
OBSERVATORY_NOTARY_KEY=<path to AuthKey_….p8> OBSERVATORY_NOTARY_KEY_ID=<key id> \
  OBSERVATORY_NOTARY_ISSUER=<issuer id> macos/scripts/notarize.sh 'dist/macos/Project Observatory.app'
# … or a `xcrun notarytool store-credentials` profile (reads the Keychain)
OBSERVATORY_NOTARY_PROFILE=<profile> macos/scripts/notarize.sh 'dist/macos/Project Observatory.app'
```

It refuses (exit 2) before anything is uploaded when the credentials are missing or
partial, or when the bundle lacks a Developer ID signature or the hardened runtime.
Otherwise it submits the bundle and waits. When Apple rejects it, it prints Apple's log and
exits 1. When Apple accepts, it staples the ticket, validates it, assesses the app with
`spctl` as Gatekeeper will, and writes `dist/ProjectObservatory-<version>-macos.zip` from the
stapled bundle. The key's path, id and issuer reach `notarytool`'s arguments and nothing
else (`tests/test_notarize_script.py`). The release attaches that zip and lists it in
`SHA256SUMS`. `full update` reads only the wheel's line, so the second line does not affect
it (`parse_sums` in `observatory/engine/engine_update.py`). The maintenance job's app step
reads the zip's line: it installs a downloaded app only when the zip matches both its GitHub
asset digest and that line, and `SHA256SUMS` carries the organization's signature
(`observatory/engine/app_update.py`).

Since 0.19.0 the app updates itself: the maintenance job stages the verified app, and the
running app offers **Restart to update** and swaps it at a safe point, never while it is in
use ([SPEC, "Updates"](SPEC.md#native-screens-and-lifecycle)).

A downloaded app keeps the browser's quarantine flag; Gatekeeper opens a notarized,
stapled copy without the "cannot be checked for malicious software" refusal. No signing
credentials or Apple agreements are supplied or accepted by automation.
