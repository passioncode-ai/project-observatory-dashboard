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

## Running git

The engine runs `git` in every watched checkout, and git executes programs its
configuration names. Every engine call goes through one module,
`observatory/engine/safe_git.py`, so that asking git a question runs nothing else:

- **The user's global and system git configuration is not loaded.** Only values
  that change what git reports or where it connects are copied from them into a
  private per-process file (mode 600, removed at exit): `safe.directory`,
  `core.excludesFile`, `core.attributesFile`, line-ending settings,
  `url.<base>.insteadOf` and HTTP proxy/CA settings. No key that names a program
  is copied.
- **What a repository's own configuration can still name is switched off** on
  every call: fsmonitor, the hooks directory and config-declared hooks
  (`hook.<name>.command`), clean/smudge/process filters, signature display and
  signing (`log.showSignature`, `commit.gpgSign`, `tag.gpgSign`), credential
  helpers (URL-scoped ones too), external diff and textconv drivers
  (`--no-ext-diff --no-textconv`), the pager and automatic `gc`. Prompts are
  disabled, askpass answers nothing, and ssh runs with `BatchMode=yes`.
- **Only the https, http, ssh, git and file transports are allowed**, so a remote
  such as `gcrypt::…` or `ext::…` is reported unreachable instead of starting a
  remote helper.
- **Scheduled commits** (the registry and the wiki projection) use the author the
  operator's own git would use in that repository, resolved with `git var`, and
  never run hooks, filters or a signer.
- **`full cleanup` removes a dirty worktree only after its archived patch has been
  re-applied, in check mode, to a scratch index of its HEAD**; a patch that cannot
  be produced or does not apply removes nothing.

Left outside, by design: `ssh` still reads `~/.ssh/config` (a `ProxyCommand` there
runs); a submodule's own configuration can name a filter driver the parent does not
and only the parent's are switched off; filters such as Git LFS are not run, so a
modified LFS file is compared byte for byte with its pointer; and values the global
file sets only under an `includeIf` condition are not copied. The regression suite
`observatory/engine/tests/test_git_hardening.py` plants a hostile `~/.gitconfig` and
repository configuration and asserts that no planted program runs. Git older than
2.32 ignores `GIT_CONFIG_GLOBAL`: there the global file is still read, and only the
per-call overrides above protect against it.

## macOS Keychain

Observatory keeps no secret in the macOS Keychain and reads none from it. Vault
slots, provider key files and the backup passphrase (`secrets/backup-passphrase`)
are files with private modes. Git runs with no credential helper (see
[Running git](#running-git)), so `osxkeychain` is never asked; a remote that wants a
password is reported unreachable, and no git signer (gpg's pinentry, an SSH signer)
is run unattended. The Mac app's dashboard view cancels every password and
client-certificate challenge rather than let WebKit consult the login keychain. Every
launch of a Chromium-family browser in a tracked script must pass
`--use-mock-keychain` and `--password-store=basic`
(`tests/test_keychain_and_app_scripts.py`).

Two things can still reach the login Keychain, each only when you turn it on:

- **Opt-in integrations that run a provider's own CLI** inherit your environment and
  that CLI's login. `gh` (the `github` integration: `collectors/scan_github.py`, and
  `collectors/merge.py` when it resolves transferred repositories) and `claude`
  (the `mcp` integration: `claude mcp list` in `collectors/scan_mcp.py`) keep
  their tokens in the login Keychain on macOS by default, so a locked keychain can show an unlock dialog when
  a scheduled scan runs them. `heroku` keeps its token in `~/.netrc`; the Heroku scan
  takes a session token from `heroku auth:token` for the run and stores none of it.
  Leave the integration off, or keep the CLI logged in with a file-based token, if no
  dialog may ever appear.
- **Signing the Mac app with a Developer ID** (`OBSERVATORY_SIGN_IDENTITY` for
  `macos/scripts/build-app.sh`) makes `codesign` read that identity's private key
  from the Keychain, which can prompt for the keychain password or for access to the
  key. That is the release operator's explicit act; the default ad-hoc signature
  touches no keychain ([docs/macos](docs/macos/README.md#signing)).

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

Use this repository's [private vulnerability-reporting channel](https://github.com/passioncode-ai/project-observatory-dashboard/security/advisories/new).
If it is unavailable, write to <contact@passioncode.ai> before sharing
sensitive details. Public issues may describe the class and a synthetic
reproduction. Never attach a real key, inventory, log, private path or customer
identifier, and do not test another person's accounts to demonstrate a report.
