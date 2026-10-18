# SSH keys on this machine (PB-163, OBS-110)

Observatory names every provider credential and never shows its value
([AGENT-SECRETS.md](AGENT-SECRETS.md)). The machine's SSH keys are the same kind of thing and were
invisible to it: `~/.ssh` holds pairs nobody inventoried, nobody knew which one GitHub or a server
accepts, which one has a passphrase, and making a key, copying its public line and adding it
somewhere took several commands, and that is the moment a private key gets pasted into the wrong window.

This page specifies one door for them. The scenarios are OSS-62–67 in
[portable-scenarios.md](../../observatory/engine/docs/ux/portable-scenarios.md#ssh-keys-on-this-machine-pb-163-obs-110).
Code: `observatory/engine/tools/ssh_keys.py` (the door and its CLI), `tools/ssh_key_findings.py`
(the board's rules), `observatory_ssh_keys` in `mcp/server.py`. Tests:
`observatory/engine/tests/test_ssh_keys.py`. Standard library only; the door runs the system's own
`ssh-keygen`, `ssh-add`, `ssh` and `gh`.

## The rule

**The private key is never shown or moved.** Only its clear header is read: the BEGIN line, and
for the OpenSSH format the fields that come before the private section (the cipher name and the
public key, which the format stores in clear). The private section is never read, and nothing read
from the file is printed, saved, logged, copied, sent or placed in an argument. Everything
the door shows, copies or sends is the public line from `NAME.pub`, after it parsed as exactly one
public key. A `.pub` file that holds anything else (a private key saved under the wrong name, two
keys, a header) is refused, and its content is not echoed in the refusal.

The public line is safe to show: it is what GitHub and every server publish. Showing it is what
makes "copy the part you paste" one step.

## Inventory (OSS-62)

`ssh_keys.inventory()` reads, without changing anything:

| Source | How | What it gives |
|---|---|---|
| `~/.ssh` | the folder's own entries, and one level of subfolders; links are reported, not followed | key pairs: `NAME` and `NAME.pub`; a private key without `.pub`; a `.pub` without a private file (a key held by an agent or a token) |
| A private key file | `open(O_NOFOLLOW)`; the BEGIN line one byte at a time (at most 64), then only as many bytes as the next field needs, see below | the format (`openssh`, `pem`, `pkcs8`), the passphrase state, and for the OpenSSH format the public key, so the fingerprint is known even without `.pub`; the file's access time is put back afterwards |
| `NAME.pub` | `ssh-keygen -l -E sha256 -f NAME.pub`, on the public file only; a link is not followed | type, bits, `SHA256:` fingerprint, comment; a `.pub` that is not the private key's own public half is marked (`mismatch`) and refused by `copy` and `send`; a `.pub` whose first line is a private-key header is a finding (`ssh.private_in_pub`) |
| `~/.ssh/config` and its `Include`s | parsed as text: `Host` and `Match` blocks, `IdentityFile` and `IdentityAgent` (with `~`, `%d`, `%u` and `${VAR}` expanded as ssh does; `%h` and the other host tokens left unresolved), depth 3; only a regular file of at most 1 MiB, opened non-blocking (a FIFO is named in `degraded`, never waited on) | which Host blocks use which key; every key an `IdentityFile` names is inventoried at any depth, in `~/.ssh` or not, and a linked one becomes a row (not followed); an `IdentityFile` that names a missing file, spelled as the config wrote it (`%u` and variables other than `HOME` are not expanded into the saved inventory) |
| ssh-agent | `ssh-add -l -E sha256`, for `SSH_AUTH_SOCK` and for every other socket an `IdentityAgent` names (1Password, Secretive) | which keys are loaded; keys loaded but on no disk; an agent that could not be read is a `degraded` row |
| GitHub | `gh api user/keys` and `gh api user/ssh_signing_keys`, one page of 100 each | which keys GitHub accepts for sign-in and for signing, by fingerprint; GitHub keys this machine does not hold |

`gh api` rather than `gh ssh-key list`: the list command has no JSON output and prints signing and
sign-in keys as display text; the API answers both kinds as JSON with the key line, from which the
fingerprint is computed (SHA-256 of the decoded key blob, unpadded base64, which is how OpenSSH
spells it). Adding still goes through `gh ssh-key add`.

**Passphrase and fingerprint without decrypting, and without reading the private section.** An
OpenSSH private key is a base64 container: `openssh-key-v1\0`, the cipher name, the kdf name, the
kdf options, the key count, the public key, and only then the private section. All of it up to the
public key is clear. The reader takes those fields one at a time, each read asking for exactly as
many base64 characters as the field the previous one announced, so the read ends with the public
key (and at most the two bytes of the next length field that share its last base64 group). Cipher
`none` means no passphrase; the public key gives the fingerprint, type and bits even when `.pub` is
missing. A field longer than 64 bytes or a public key longer than 4 KB is not read: the key is
`unknown`. A PEM key: the BEGIN line, then exactly ten bytes; `Proc-Type:` there means an
encrypted key (the rest of that line is read to see `ENCRYPTED`), anything else means none, and
nothing more is read. PKCS#8 says `ENCRYPTED PRIVATE KEY` in its first line, and nothing after it
is read. A file whose first byte is not `-` costs one byte. The door never runs `ssh-keygen -y`
(which decrypts) and never offers an empty passphrase to test one. `HeaderReadIsBounded` in the
tests spies on every `os.read` and holds the total under the offset of the private section's first
base64 character, for ed25519, RSA-4096, an encrypted key and a FIDO key.

**Last use, estimated.** ssh reads a private key when it uses it, which updates the file's access
time on file systems that keep one. That is the only local trace, so the inventory reports it as
`last_access` and the finding calls it an estimate. Reading the header changes that time too, so
the door puts it back (`os.utime` with the measured access and modification times).

**What was not seen (`unseen`).** A key this machine uses can live where the inventory cannot read
it: in an agent the config names that is not running, behind a linked key file, at an `IdentityFile`
whose path depends on the host (`~/keys/%h`) or on an unset variable, in a file that does not parse
as a key, a file or folder whose look failed (EACCES, ELOOP, EIO: `list` never stops on one), an
`IdentityFile` of an unknown `~user`, or a private key whose fingerprint is unknown. Each such gap is a
line of `unseen`. An empty listing says "No SSH keys yet" only when nothing went unseen and no local
source (an agent, the config) is in `degraded`; otherwise it says "No SSH keys found where Observatory
could look" and names what it could not see.

**Decision (review round 3): Observatory never advises deleting a GitHub key.** Three review rounds
in a row each found another place a key in use can live where this inventory cannot see it (an agent
socket, a linked, deep, dot or unreadable file, a host-dependent or `${VAR}` path, an `Include` it did
not follow, a stale saved answer). Absence cannot be proven from one machine, so `ssh.github_orphan`
is always severity info and carries no command and no action field. Its text says that GitHub accepts
the key (title and fingerprint), that Observatory did not find it in the places it read on this machine,
names those places and the ones it could not read, and ends: "if no machine of yours uses it, remove
it in GitHub settings". Nothing in the CLI, the board or the MCP answer leads a person or an agent to
delete a GitHub key.

`list` saves the inventory to `store/raw/ssh-keys.json`, owner-only, with paths spelled from `~`.
The board reads that file; the MCP tool measures the local sources live.

## Create (OSS-63)

```
project-observatory full ssh-keys create NAME [--comment TEXT] [--no-passphrase] [--no-keychain]
                                              [--host ALIAS --hostname HOST [--user USER] [--port N]]
```

1. `NAME` is `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, not ending in `.pub`, and not a name ssh reads as
   something else (`config`, `rc`, `environment`, `authorized_keys*`, `known_hosts*`, `*-cert`). `~/.ssh/NAME` and
   `~/.ssh/NAME.pub` must not exist, as files or links. With `--host`, the alias must not be a Host
   of `~/.ssh/config`, and the config must not be a link. Every check runs before anything is
   generated, so a refusal leaves nothing behind.
2. `~/.ssh` is created owner-only (`osprivacy.make_private`: 0700, an owner-only ACL on Windows) if missing. An existing folder keeps its mode; a mode wider
   than 0700 is a finding, not something this command changes. A `~/.ssh` that is a link is refused:
   nothing is written through a link.
3. `ssh-keygen -q -t ed25519 -a 100 -C COMMENT -f TMP/NAME` runs in a private temporary folder
   inside `~/.ssh`. Without `--no-passphrase` it inherits the terminal and asks for the passphrase
   itself; a run with no terminal is refused before it starts ("a passphrase is typed by a
   person"). The passphrase is never an argument: `-N` is passed only as `-N ""` for
   `--no-passphrase`. The comment defaults to `NAME`, so a public key published on GitHub does not
   carry the machine's host name.
4. Both files are linked into `~/.ssh` with `os.link`, which fails if the name exists: a writer that
   raced in between keeps its file, and this run removes what it linked and reports the race. The
   private file is made owner-only (`osprivacy.make_private`), the public one 0644. The temporary
   folder is removed.
5. On macOS, unless `--no-passphrase` or `--no-keychain`, `ssh-add --apple-use-keychain` stores
   the passphrase in the login Keychain (it asks for it once) and also loads the key into the running
   agent; the answer says both. Elsewhere the key is not added to an agent, and the answer says so.
   **Decision (round 1, F13):** the Keychain step stays on by default on macOS: it is what makes a
   new key usable at once, which is the operator's "quickly"; `--no-keychain` opts out.
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
- Otherwise `gh ssh-key add - --type TYPE --title=TITLE`: the validated public line goes on stdin,
  and the title is one argument (`--title=…`), so a title that looks like an option stays a title.
  The title defaults to the key's comment.
- `gh` missing, signed out, or without the scope: refused before anything is sent, with
  `gh auth refresh -h github.com -s admin:public_key` (`admin:ssh_signing_key` for signing).
- A provider refusal is shown as GitHub said it, control characters removed, anything shaped like a
  credential (`credential_shape`) replaced, at most 300 characters.

`full ssh-keys send NAME --to server:ALIAS [--verify] [--interactive]`

- `ALIAS` is a Host of `~/.ssh/config` or `[user@]host`; it may not start with `-` and is passed
  after `--`, so it cannot become an ssh option.
- The access that already works: `ssh -o BatchMode=yes -o ConnectTimeout=15 -- ALIAS 'sh -s -- LINE'`,
  with the script on stdin. The remote login shell (csh, fish) parses only that one quoted command;
  the script itself runs under `sh`. `--interactive` drops `BatchMode` so a person can answer a
  password prompt.
- The script checks the line looks like a key, creates `~/.ssh` (0700) and `authorized_keys` (0600)
  if missing, and appends the line only when no line already holds the same key blob, after adding a
  newline if the file does not end with one. Its exit code decides: 0 added, 10 already there,
  anything else a failure, whatever its words on stdout say. It never removes or rewrites a line.
- `--verify` proves the server accepts **the new key and no other**. A plain `ssh -o
  IdentitiesOnly=yes -i NEW ALIAS true` does not: `IdentityFile` lines of the config accumulate with
  `-i`, so a Host block naming the old key logs in with the old key and the check passes (round-1
  finding F1). So the host is resolved once with `ssh -G ALIAS` (host name, port, user, ProxyJump
  hops, known-hosts settings), and the login runs with `-F none` (no config at all), `-i NEW` only,
  `IdentitiesOnly=yes`, `PreferredAuthentications=publickey`, `IdentityAgent=none`, `-p`, `-l` and
  `-J` from the resolution, and `-v`. It passes only when it exits 0 **and** ssh's own log line
  `Server accepts key: … SHA256:…` names the new key's fingerprint. A key with a passphrase, run
  without a person at the terminal, keeps the agent (it may hold the key unlocked); `-F none` with
  `IdentitiesOnly` still offers that one key only, and the fingerprint check holds either way. A host
  reached through a `ProxyCommand` is not verified (that command cannot be reproduced without the
  config): the answer says so and gives the command to check by hand. The argument is the key's
  path, never its content.

`--to digitalocean` and any other cloud: Observatory's DigitalOcean door handles database
credentials through `doctl` and has no SSH-key command yet, and no other cloud has a door. The
send is refused with that gap named (OBS-112) and the copy command as the way meanwhile. A door,
when it lands, reads the provider's keys by fingerprint first (DigitalOcean spells them as MD5),
imports through the CLI that already holds the access, and writes the same receipts.

**Which key the agent may sign with (review round 3).** The agent is offered during `--verify` only
when the private file's own header gives a fingerprint equal to the `.pub`'s. A PKCS#8 or PEM key
stores no public key in clear, so a `.pub` holding another key B passed the pair check, and the agent
holding B signed for it: the server named B, which was the `.pub`'s fingerprint, and the verify passed.
Without that proof the login uses `IdentityAgent=none`; a key with a passphrase then fails in batch mode
with the hint to run `--interactive`, and a pass says "the private file was not checked" instead of
"alone". All agent reads of one inventory share a 30 s budget, each at most 15 s; one that does not
answer says "did not answer within N s".

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
| `ssh.key_no_passphrase` | warning; info for a FIDO (`*-SK`) handle, which is useless without the security key | the private key's cipher is `none` |
| `ssh.key_weak` | critical for DSA and RSA under 2048 bits, warning for RSA under 3072 | the type and bits from `ssh-keygen -l` |
| `ssh.key_readable` | critical | `osprivacy.private` says the private file is not owner-only (on POSIX a group or other bit, 0640 included; on Windows the ACL, never the raw 0o666 mode Windows reports) |
| `ssh.folder_permissions` | warning | `osprivacy.private` says `~/.ssh` is not owner-only |
| `ssh.private_in_pub` | critical | a `.pub` file's first line is a private-key header, read from that line alone |
| `ssh.key_unused` | info | not loaded in the agent and last accessed over 180 days ago (estimated) |
| `ssh.github_orphan` | always info; no command, no action field (decision of round 3) | GitHub accepts a key Observatory did not find in the places it read; the row names those places and the ones it could not read |
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

- Read a private key's private section, decrypt it, or ask for its passphrase.
- Print a file name, a config value or a provider's message to the terminal without the printable
  filter (an escape sequence in a name could drive the terminal).
- Print, save, log, copy or send a private key, or pass one as an argument.
- Overwrite a key file, or delete one.
- Change the mode of an existing `~/.ssh` or key file (a finding says what to run).
- Remove a line from a server's `authorized_keys` (OBS-112's rotation, after verification, is the only
  path that will).
- Advise deleting a GitHub key, or print or return a `gh ssh-key delete` command or any delete action
  for one. OBS-112's rotation removes the OLD key's registration only after the new key was verified,
  and only the key the person named.
- Store an SSH private key in the vault or move it to another machine (PB-163's non-goal).
- Manage other people's `authorized_keys`, or GPG keys.

## Surfaces

| Reader | Surface |
|---|---|
| A person | `project-observatory full ssh-keys [list] \| show NAME \| copy NAME \| create NAME \| send NAME --to … \| findings` (`--json` on `list` and `findings`); `full --help`; [ONBOARDING.md](../ONBOARDING.md#ssh-keys-on-this-machine) |
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
