# SSH keys on this machine (PB-163, OBS-110)

Observatory names every provider credential and never shows its value
([AGENT-SECRETS.md](AGENT-SECRETS.md)). The machine's SSH keys are the same kind of thing and were
invisible to it: `~/.ssh` holds pairs nobody inventoried, nobody knew which one GitHub or a server
accepts, which one has a passphrase, and making a key, copying its public line and adding it
somewhere took several commands — the moment a private key gets pasted into the wrong window.

This page specifies one door for them. The scenarios are OSS-62–67 in
[portable-scenarios.md](../../observatory/engine/docs/ux/portable-scenarios.md#ssh-keys-on-this-machine-pb-163-obs-110).
Code: `observatory/engine/tools/ssh_keys.py` (the door and its CLI), `tools/ssh_key_findings.py`
(the board's rules), `observatory_ssh_keys` in `mcp/server.py`. Tests:
`observatory/engine/tests/test_ssh_keys.py`. Standard library only; the door runs the system's own
`ssh-keygen`, `ssh-add`, `ssh` and `gh`.

## The rule

**The private key is never shown or moved.** No byte of it past its first two lines is read, and
nothing read from it is printed, saved, logged, copied, sent or placed in an argument. Everything
the door shows, copies or sends is the public line from `NAME.pub`, after it parsed as exactly one
public key. A `.pub` file that holds anything else — a private key saved under the wrong name, two
keys, a header — is refused, and its content is not echoed in the refusal.

The public line is safe to show: it is what GitHub and every server publish. Showing it is what
makes "copy the part you paste" one step.

## Inventory (OSS-62)

`ssh_keys.inventory()` reads, without changing anything:

| Source | How | What it gives |
|---|---|---|
| `~/.ssh` | the folder's own entries, and one level of subfolders; links are reported, not followed | key pairs: `NAME` and `NAME.pub`; a private key without `.pub`; a `.pub` without a private file (a key held by an agent or a token) |
| A private key file | `open(O_NOFOLLOW)`, at most 512 bytes; the header line, and for the OpenSSH format the cipher name at the start of the next line | the format (`openssh`, `pem`, `pkcs8`) and the passphrase state, see below; the file's access time is put back afterwards |
| `NAME.pub` | `ssh-keygen -l -E sha256 -f NAME.pub`, on the public file only | type, bits, `SHA256:` fingerprint, comment |
| `~/.ssh/config` and its `Include`s | parsed as text: `Host` and `Match` blocks, `IdentityFile` (with `~` and `%d` expanded), depth 3 | which Host blocks use which key; an `IdentityFile` that names a missing file |
| ssh-agent | `ssh-add -l -E sha256` | which keys are loaded; keys loaded but on no disk |
| GitHub | `gh api user/keys` and `gh api user/ssh_signing_keys`, one page of 100 each | which keys GitHub accepts for sign-in and for signing, by fingerprint; GitHub keys this machine does not hold |

`gh api` rather than `gh ssh-key list`: the list command has no JSON output and prints signing and
sign-in keys as display text; the API answers both kinds as JSON with the key line, from which the
fingerprint is computed (SHA-256 of the decoded key blob, unpadded base64, which is how OpenSSH
spells it). Adding still goes through `gh ssh-key add`.

**Passphrase without decrypting.** An OpenSSH private key starts with `openssh-key-v1\0`, then the
cipher name in clear: `none` means no passphrase. Only the first 68 base64 characters of that line
are decoded, which reach the cipher name and nothing after it (the key material starts after the
public key, far later). A PEM key says `Proc-Type: 4,ENCRYPTED` in its header; PKCS#8 says
`ENCRYPTED PRIVATE KEY` in its first line. Anything else is `unknown`, never a guess. The door never
runs `ssh-keygen -y` (which decrypts) and never offers an empty passphrase to test one.

**Last use, estimated.** ssh reads a private key when it uses it, which updates the file's access
time on file systems that keep one. That is the only local trace, so the inventory reports it as
`last_access` and the finding calls it an estimate. Reading the header changes that time too, so
the door puts it back (`os.utime` with the measured access and modification times).

`list` saves the inventory to `store/raw/ssh-keys.json`, owner-only, with paths spelled from `~`.
The board reads that file; the MCP tool measures the local sources live.

## Create (OSS-63)

```
project-observatory full ssh-keys create NAME [--comment TEXT] [--no-passphrase] [--no-keychain]
                                              [--host ALIAS --hostname HOST [--user USER] [--port N]]
```

1. `NAME` is `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, not ending in `.pub`. `~/.ssh/NAME` and
   `~/.ssh/NAME.pub` must not exist, as files or links. With `--host`, the alias must not be a Host
   of `~/.ssh/config`, and the config must not be a link. Every check runs before anything is
   generated, so a refusal leaves nothing behind.
2. `~/.ssh` is created with mode 0700 if missing. An existing folder keeps its mode; a mode wider
   than 0700 is a finding, not something this command changes.
3. `ssh-keygen -q -t ed25519 -a 100 -C COMMENT -f TMP/NAME` runs in a private temporary folder
   inside `~/.ssh`. Without `--no-passphrase` it inherits the terminal and asks for the passphrase
   itself; a run with no terminal is refused before it starts ("a passphrase is typed by a
   person"). The passphrase is never an argument: `-N` is passed only as `-N ""` for
   `--no-passphrase`. The comment defaults to `NAME`, so a public key published on GitHub does not
   carry the machine's host name.
4. Both files are linked into `~/.ssh` with `os.link`, which fails if the name exists: a writer that
   raced in between keeps its file, and this run removes what it linked and reports the race. Modes
   are set to 0600 and 0644. The temporary folder is removed.
5. On macOS, unless `--no-passphrase` or `--no-keychain`, `ssh-add --apple-use-keychain` stores
   the passphrase in the login Keychain (it asks for it once). Elsewhere the key is not added to an
   agent, and the answer says so.
6. With `--host`, a block is appended to `~/.ssh/config` (created 0600 if missing): `Host`,
   `HostName`, `User`, `Port`, `IdentityFile ~/.ssh/NAME`, `IdentitiesOnly yes`, `AddKeysToAgent
   yes`, and `UseKeychain yes` on macOS only (other systems' ssh refuses that option).
7. A receipt `ssh.created` (OSS-65's receipts) and the answer: path, fingerprint, public line, the
   copy and send commands.

## Copy the public part (OSS-64)

`full ssh-keys copy NAME` reads `NAME.pub`, checks it is one public key line (a known type, a blob
that decodes and starts with that type, an optional comment without control characters), writes the
line to the clipboard on stdin (`pbcopy`; elsewhere `wl-copy`, then `xclip -selection clipboard`)
and prints it with its fingerprint. No clipboard tool: the line is printed and exit 3 says the
clipboard was not set. `full ssh-keys show NAME` prints without the clipboard.

## Send (OSS-65)

`full ssh-keys send NAME --to github [--as authentication|signing|both] [--title TEXT]`

- Reads GitHub's keys of that use first. A key with the same fingerprint is `already registered`.
- Otherwise `gh ssh-key add ~/.ssh/NAME.pub --type TYPE --title TITLE` (the title defaults to the
  key's comment). The argument is the public file's path.
- `gh` missing, signed out, or without the scope: refused before anything is sent, with
  `gh auth refresh -h github.com -s admin:public_key` (`admin:ssh_signing_key` for signing).
- A provider refusal is shown as GitHub said it, control characters removed, anything shaped like a
  credential (`credential_shape`) replaced, at most 300 characters.

`full ssh-keys send NAME --to server:ALIAS [--verify] [--interactive]`

- `ALIAS` is a Host of `~/.ssh/config` or `[user@]host`; it may not start with `-` and is passed
  after `--`, so it cannot become an ssh option.
- The access that already works: `ssh -o BatchMode=yes -o ConnectTimeout=15 -- ALIAS 'sh -c …'`.
  `--interactive` drops `BatchMode` so a person can answer a password prompt.
- The public line goes over stdin to a short POSIX script: it checks the line looks like a key,
  creates `~/.ssh` (0700) and `authorized_keys` (0600) if missing, and appends the line only when no
  line already holds the same key blob, after adding a newline if the file does not end with one.
  It answers `added` or `already`. It never removes or rewrites a line.
- `--verify` logs in with the new key alone: `ssh -o BatchMode=yes -o IdentitiesOnly=yes -i
  ~/.ssh/NAME -- ALIAS true`. The argument is the key's path, never its content.

`--to digitalocean` and any other cloud: Observatory's DigitalOcean door handles database
credentials through `doctl` and has no SSH-key command yet, and no other cloud has a door. The
send is refused with that gap named (OBS-112) and the copy command as the way meanwhile. A door,
when it lands, reads the provider's keys by fingerprint first (DigitalOcean spells them as MD5),
imports through the CLI that already holds the access, and writes the same receipts.

## Receipts

Every create and every send writes one row to the key movements journal
(`<secret store>/projects/movements.jsonl`, `vault.journal`), the same journal every door writes:
`event` (`ssh.created`, `ssh.registered`, `ssh.already_registered`, `ssh.register_failed`,
`ssh.verified`, `ssh.verify_failed`), `secret` = `ssh/SHA256:…`, `to` (`github:authentication`,
`github:signing`, `server:ALIAS`), `how`, `by` and `at`. Never a key line. A failure to write the
receipt after the action succeeded exits 4 and says the action happened.

## Findings (OSS-66)

`tools/ssh_key_findings.py` reads the saved inventory; `tools/build_findings.py` adds its rows to
the board, and `full ssh-keys findings` prints them.

| Type | Severity | When |
|---|---|---|
| `ssh.key_no_passphrase` | warning | the private key's cipher is `none` |
| `ssh.key_weak` | critical for DSA and RSA under 2048 bits, warning for RSA under 3072 | the type and bits from `ssh-keygen -l` |
| `ssh.key_readable` | critical | the private file's mode allows group or others (ssh itself refuses such a key) |
| `ssh.folder_permissions` | warning | `~/.ssh` is readable or writable by group or others |
| `ssh.key_unused` | info | not loaded in the agent and last accessed over 180 days ago (estimated) |
| `ssh.github_orphan` | warning | GitHub accepts a key this machine neither holds nor has loaded |
| `ssh.key_unregistered` | info | GitHub was read, and the key is not on GitHub, in no Host block and not loaded |
| `ssh.public_missing` | info | a private key without its `.pub` |
| `ssh.identity_missing` | warning | an `IdentityFile` names a file that does not exist |

Rows are one per key, keyed `ssh:SHA256:…` (or `ssh:~/.ssh/NAME` without a fingerprint). No row
carries a timestamp of its own beyond the inventory's.

## Rotation (OSS-67, OBS-112)

Specified here, shipped with OBS-112: `ssh-keys rotate OLD NEW` creates NEW, registers it where
OLD is registered (the GitHub uses; the servers whose Host blocks name OLD), verifies each, and only
then removes OLD's registrations (`gh ssh-key delete`; the matching line of `authorized_keys`, by
key blob) and points the Host blocks at NEW. OLD's private file is moved to `~/.ssh/retired/`,
never deleted. Each step is a receipt; a failed verification stops before any removal and names
the step to retry.

## What is never done

- Read a private key past its header, decrypt it, or ask for its passphrase.
- Print, save, log, copy or send a private key, or pass one as an argument.
- Overwrite a key file, or delete one.
- Change the mode of an existing `~/.ssh` or key file (a finding says what to run).
- Remove a line from a server's `authorized_keys`, or a key from GitHub (OBS-112's rotation, after
  verification, is the only path that will).
- Store an SSH private key in the vault or move it to another machine (PB-163's non-goal).
- Manage other people's `authorized_keys`, or GPG keys.

## Surfaces

| Reader | Surface |
|---|---|
| A person | `project-observatory full ssh-keys list \| show NAME \| copy NAME \| create NAME \| send NAME --to … \| findings` (`--json` on `list` and `findings`); `full --help`; [ONBOARDING.md](../ONBOARDING.md#ssh-keys-on-this-machine) |
| An agent | `observatory_ssh_keys` (read-only: the inventory, one key's public line with `name`, the findings and the commands); the same CLI; [AGENT-ONBOARDING.md](../AGENT-ONBOARDING.md#ssh-keys) |
| The board | the findings above, from `store/raw/ssh-keys.json` |
| The dashboard | a section on the keys page: OBS-111 |

## Degradation

Each source that could not be read is a `degraded` row with its reason and the command that fixes
it: `ssh-keygen` missing (no fingerprints), ssh-agent not running or not reachable, `gh` missing,
signed out, or missing `read:public_key` / `read:ssh_signing_key`, a GitHub page of exactly 100
keys (only the first page was read), a config `Include` deeper than three levels or unreadable. An
empty listing with a degraded source never reads as "no keys". Exit codes: 0 done; 1 the provider
or the server refused, or verification failed; 2 refused before anything changed; 3 done without
the clipboard; 4 done, receipt not written.
