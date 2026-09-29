# Security and privacy boundary

Project Observatory observes the projects and sources its user configures. It
is not isolation from the same operating-system user, malicious plugins or child
processes, malware, or a privileged administrator. A local agent with filesystem
access can still read files that the operating system permits it to read.

Who can reach which local surface, what grants it and which test proves each rule:
[docs/design/ACCESS.md](docs/design/ACCESS.md).

## Public code and private data

The full engine lives under `observatory/engine`. Its versioned workspace lives
outside source and contains configuration, project metadata, registries, SQLite
history, keys, journals, backups and generated dashboards. These artifacts are
private even when they contain only names or fingerprints. Default credential
files are plaintext with private POSIX permissions, not an encrypted vault.
Externally configured secret stores are outside workspace backups.

Never publish runtime state, source exports from providers, transcripts, real
screenshots, or private installation history. `site/` alone is the public static
artifact. The release gate checks allowlisted paths, credential patterns,
reviewed image hashes, source metadata and public Git history. A separate local
private-identifier list is used during maintainer review and is never committed.
No scanner can prove the absence of every personal fact or credential shape.

## Input and output

Full-engine project secrets enter `tools/vault.py` through stdin. Ordinary
listing and movement records contain names and metadata. `tools/use_secret.py`
provides values to one child process and filters exact known values from its
captured output. Encodings, transformations, direct network transmissions and
files written by that child are outside this filter. Run only trusted commands.

The credential UI can reveal a selected inventoried value after an explicit,
authenticated local request. Reveal is deliberately different from a report;
users must not copy its result into an agent transcript or public issue.
Free-form incident notes are user input, not a guarantee that arbitrary secrets
inside a note will be recognized and redacted. Never include a value in a note.

Named-key management at a provider changes a live account. Issuance, delivery,
local rotation and provider revocation are separate outcomes; a local replacement
must not be presented as proof that the old credential was revoked.

## Observation and effects

`full local` uses an offline scope and invokes no provider or executable metric
plugin. Other explicitly selected commands can call configured providers.
Integrations, paid interpretation, embeddings, scheduling, notifications,
retention, memory remediation and projections require their documented opt-ins.
User-added metric plugins are trusted executable code, not sandboxed extensions.
Their manifest API/version and path checks do not make hostile code safe.

The full engine preserves original observation and remediation capabilities.
Read each command's scope before use. Memory remediation has its own opt-in and
private backups; removal from selected live stores is not a promise to erase
past backups, other databases or remote copies.

## Local HTTP boundary

The full dashboard services bind loopback and validate the exact Host and Origin
before protected actions. The keyserver requires a local token for credential
operations and rejects malformed requests before provider effects. UI pages and
local overview endpoints still expose private metadata to processes able to
reach them. Do not reverse-proxy these services onto a public network or treat
them as a multi-tenant service. The always-on dashboard server's events feed
(`/fabric/v1/events`) requires the workspace's `service.token` (mode 600) as
`Authorization: Bearer`; its pages and its `/.well-known/fabric-service` identity
document stay open to local reads. See [the service design](docs/design/FABRIC-SERVICE.md).

The retained 0.1 `serve` command remains a separate read-only local overview,
without the full keyserver's reveal or provisioning features. Its child-secret
runner suppresses output rather than using the full engine's streaming filter.
The two interfaces have distinct state formats and security tests.

## Upgrades and backups

Unknown future workspace/config/database formats are refused. Supported database
migrations use verified SQLite snapshots including committed WAL data. Stop all
writers first; older executables cannot honor new locks. Restore into a new home
and use the matching application version. Protect snapshots as secrets, and
back up external configured stores independently. See
[compatibility](docs/COMPATIBILITY.md) for supported contracts and recovery.

## Detection limits

Known-value matching detects copies of locally known values in the inspected
artifacts. Unknown, encoded, transformed, split or rotated-away values may not
be covered. Missing inputs and partial results must not be interpreted as clean
results. Several slots can hold one credential; occurrences, unique credentials
and incidents are different units.

An occurrence in a local transcript or memory database proves that a value
reached that artifact. It does not prove exfiltration, a vendor breach or a
vulnerability in the storage library. Public examples are synthetic unless
explicitly identified as a separately reviewed historical observation.

## Report an issue privately

Use this repository's private vulnerability-reporting channel. If unavailable,
contact the maintainer through [sshlg.me](https://sshlg.me/) before sharing
sensitive details. Public issues may describe the class and a synthetic
reproduction. Never attach a real key, inventory, log, private path or customer
identifier, and do not test another person's accounts to demonstrate a report.
