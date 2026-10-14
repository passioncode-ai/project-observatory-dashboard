# Agent secrets: every credential an agent uses comes from Observatory

**The rule.** Every credential an agent uses — a one-shot agent, a long-running agent service, or
code an agent writes for one — is issued, stored and named in Project Observatory, discovered by
name, and injected at run time. An agent has no `.env` of its own, no key file beside its data,
and never a value in code, configuration, a commit, a prompt, a log or agent memory.

Why one home: a key with one home can be rotated, its exposure recorded and settled, its
consumers listed, and its leak found by an exact-value scan. A key copied into an agent's own file
drifts from the vault on the first rotation, is invisible to the leak scan's list of known values
until someone inventories it, and survives the agent that needed it.

## How an agent follows it today

| Step | Command or tool | Shipped |
|---|---|---|
| Issue a key from a provider straight into a slot | `cloudflare.py issue --vault PROJECT/ENV/NAME`, `openrouter.py issue --to vault:PROJECT/ENV/NAME` | yes |
| Issue a project's own key from its spend policy (OBS-58): every `project-key` source becomes a key issued for that project into the slot it names; the keys' monthly ceilings together are at most the project limit (ENG-002) — split equally, rounded down to the cent, unless `--split` gives each slot its amount or `--limit` one ceiling per key; another project's slot is refused as a borrow; a held slot is read at the provider, and a disabled or deleted key there is `held-disabled` or `held-missing` (exit 1), never in place; ceilings that go down move first; a failure halfway exits 1 with `partial` and the steps already done; a second run moves only a changed ceiling. Only `issue-for-policy` upholds "together at most the project limit": the keys page's ceiling action and `openrouter.py limit` can raise one key above its share, and the next `issue-for-policy` run brings the keys back under the limit. `--dry-run` reads the provider for every held slot: with no stashed admin for a slot's account (or a row naming none while several are stashed and no `--account`), the slot reads `held-unread` and the run exits 1. After a ceiling moves, set `keyCeilingsUsd` in `config/meter.json` to the amounts the plan prints. The total holds at every moment: every key under `vault:<project>/` counts, a key the policy no longer names is reported `not-in-policy` and not moved, an unread key counts at its ledger ceiling, raises run only when the total after the run fits (else `blocked`, exit 1), and one run holds a lock beside the ledger at a time. Totals are whole cents. A key minted whose delivery was never recorded keeps a pending ledger row that holds its slot (`held-pending`, nothing minted there again). A run that leaves the total past the limit prints `OVER LIMIT by $X now ($A against the $L limit); it ends after: revoke A, …` (`OVER LIMIT: unbounded now` when a key has no ceiling) and exits 1. **The one exception to the invariant is `rotate`** (and `rotate --leaked`): it delivers the successor before it deletes the predecessor, so while it runs the slot holds two live keys and the project's total can reach the slot's ceiling twice over. Until the J-3 change counts them, a rotate that FAILS (a provider error after the mint, a timeout, a kill, a vault put that fails, a predecessor delete that fails) can leave the successor `NAME-next` live and untracked, or the predecessor live and uncounted (the row then reads `rotation_status: successor-delivered-predecessor-pending` with its `predecessor_hash`); `issue-for-policy` does not see either. The remedy: at the provider, delete whichever of the two keys the slot does not hold, then run `issue-for-policy`. A mint the provider answered with a 5xx or a timeout keeps its pending row, because the key may exist; past the listing, where the name cannot be looked up, `revoke NAME --forget-pending --reason "…"` clears a pending row with no hash once the key is gone; its reason must first be recorded as an attestation, or the row stays. The attestation is not provider verification; completion is a separate event after the ledger write | `openrouter.py issue-for-policy --policy FILE [--limit USD \| --split REF=USD,…] [--dry-run]` | yes |
| Read what one issued OpenRouter key cost, by UTC day and model, from the provider's activity report (INB-019); the key's hash is sent, never printed; a refused report is an error, never $0 | `openrouter.py spend NAME [--date YYYY-MM-DD] [--json]` | no |
| Store a key the user has | `vault.py put PROJECT ENV NAME`, value on stdin | yes |
| Find what exists | `observatory_credentials` (MCP) or `use_secret.py names PROJECT`; names only | yes |
| Run with it, from the vault only | `use_secret.py run --vault-only [--env ENV] PROJECT NAME -- COMMAND` | yes (`--vault-only` new) |
| Make vault-only the default for an agent | `OBSERVATORY_VAULT_ONLY=1` in the agent's environment | yes (new) |
| Say what a workflow needs | checkpoint `credentials: [{project, env, name, purpose}]` | yes (new) |
| Know before a handoff that a key is missing | the pack and the acceptance carry each credential's state and `credentialsMissing` | yes (new) |
| Run a long-lived service with its keys | `use_secret.py serve [--env ENV] --consumer LABEL PROJECT NAME[,…] -- COMMAND`: vault only, then `exec` — the service is the process, gets launchd's signals and keeps its own output | yes (new) |
| Release a slot only under a grant that names it | `FABRIC_DELEGATION_TOKEN` in the agent's environment: its token from Fabric's hub (aud `project-observatory`, scope `secret.use`); `run`, `serve` and `header` then release a vault slot only when a grant the token names has `credentialSource {kind: project-key, ref: PROJECT/ENV/NAME}`, never read a `.env`, and remove the token before the child starts (`docs/design/METER.md` M5) | yes (new; checked when a token is present; never demanded on the operator's machines, METER.md M0) |
| Put a DigitalOcean database's connection URL in a slot, and rotate its password with every named consumer | `digitalocean.py db credential CLUSTER USER --db NAME --to vault:PROJECT/ENV/NAME`; `digitalocean.py db rotate … --app ID --spec-env ID:KEY` (doctl's current context is the credential; `db consumers CLUSTER` lists the App Platform apps that bind the cluster) | yes (new) |
| Give an MCP server its key as a header, never in the client's config | `vault.py bind PROJECT ENV NAME --header-for URL` (https, or `http://127.0.0.1:<port>` / `http://[::1]:<port>` for a loopback server; `localhost` refused), then `use_secret.py header PROJECT NAME` as the client's helper: Claude Code's `headersHelper`, or Codex's `http_headers_helper` with `--server-url URL`, since Codex exports no server URL; two URLs that disagree are refused (`url-conflict`) and the audit row names the caller | yes (loopback and `--server-url` new) |
| Be told which services to restart after a rotation | `vault.py rotate` lists each recorded consumer with its `launchctl kickstart -k` command | yes (new) |

## What the commit gate refuses

`tools/check_secrets.py` judges the engine folder's tracked files (`git ls-files -z` under
`observatory/engine/`, so a non-ASCII name is the file's own name), each read as bytes and decoded as UTF-8 with any other byte kept
(`surrogateescape`); a file that will not read is a failure, never a skip. The scan reads the WORKING
TREE, not the index: every folder on a path is opened `O_NOFOLLOW|O_DIRECTORY` relative to the one before
it, so nothing is read through a link; a path under a folder that is a symbolic link is unreadable (a
committed key whose folder was swapped for a link to a harmless copy fails the gate; the copy is not read
in its place). A tracked symbolic link is judged by its target TEXT (what git stores for a link) and never
followed; any other path that is not a regular file (a FIFO, a device, a folder) is unreadable; a regular
file is opened `O_NOFOLLOW|O_NONBLOCK`, checked with `fstat` and read up to 32 MB (a larger one is
unreadable, named so). Every open goes through `osprivacy`; on Windows, which has no folder descriptor,
each folder on the path is `lstat`ed and a link or junction refused before the file is opened by path
(a window between that look and the open stays there, which `dir_fd` closes on POSIX). A skip-worktree entry (a sparse checkout) and a tracked file missing from the
working tree are read from their index blob (`git cat-file`, with `GIT_NO_REPLACE_OBJECTS=1`, so a `refs/replace/`
stand-in for the blob is not read in its place); a gitlink (a submodule) is skipped and
named, its own repository not scanned here. File NAMES are never judged, only contents. `--history N` reads `git log -p --root --full-history --no-renames --text
--diff-merges=first-parent --src-prefix=a/ --dst-prefix=b/ --relative -- .` with `GIT_NO_REPLACE_OBJECTS=1` and `GIT_GRAFT_FILE` set to an empty file, and **refuses a shallow repository** (exit 1, "history
incomplete": a `.git/shallow` boundary would hide every commit behind it), (non-ASCII names unquoted) the same way — so a file
holding a NUL or marked `binary`/`-diff` in `.gitattributes` is read as text, a merge's resolution is
diffed against its first parent, a repository's `diff.noprefix` cannot hide a real `b/` folder, `log.showRoot=false` cannot hide the
root commit, a `refs/replace/` stand-in or a `.git/info/grafts` entry cannot hide the commit that added a key, and a side branch
that added a key and removed it before its merge is still read. N counts commits in git's date order,
side-branch commits included; a renamed or copied file shows its whole content as added lines (also when
it leaves a `SKIP` path, and whatever `diff.renames` says) — splits it on `\n` only, takes a `+++` line as a file's name only
between its `diff --git` and its first `@@` (inside a hunk `+++ b/x` and `+++i;` are added lines), unquotes
a C-quoted name, and applies `SKIP` and `ALLOWLIST` by the same paths as the tree scan. A byte-order mark at the start or at any line start,
and a lone CR, U+2028, U+2029, NEL, form feed or vertical tab, are read as line structure. **Every rule judges every value, and at one position
the most severe verdict wins: refused, then fixture, then not judged** — a fixture verdict never hides a
refusal another rule makes of the same value. Its tests pin every row
(`observatory/engine/tests/test_secrets.py`: `test_round5_grammar_cases`, `test_round6_counterexamples`,
`test_round5_seeded_generator` — 2,400 keys — `test_round6_generator_crlf_and_non_utf8` — 1,200 CRLF and
non-UTF-8 cases — `test_round7_generator_overlaps_bom_and_cr` — 1,200 overlaps — `test_round8_leading_underscores_and_the_cheap_lows`,
`test_round9_nested_names_slots_and_the_cheap_lows`, `test_round9_history_headers_are_a_state_machine`, the four `test_round10_…` tests, the seven `test_round11_…` tests, the seven `test_round13_…` tests — 12,000 seeded 32- and 40-hex values — the four `test_round14_…` tests, and
`test_round5_out_of_scope_is_documented_not_refused`).

| | Rule | Example shape |
|---|---|---|
| **Refuses** | A **token**: a known issuer prefix — `credential_shape.PREFIXES`, `sk-or-v1-`, `sk-proj-`, `sk-ant-api03-`; the **longest** prefix wins — at a **token start**: the start of the text, any character other than an ASCII letter, ASCII digit or `_` (so a non-ASCII letter such as `é` right before the prefix still starts a token), or a literal `\n`/`\t`/`\r` escape in a string. The token runs to the longest `[A-Za-z0-9_-]` run, and its body is refused at or above the prefix's minimum (`credential_shape.BODY_MIN` where it names one — above or below 24 — else 24), whatever its entropy | `ghp_<36>`, `sk_live_<64 hex>`, `glpat-<20>`, `ghp_` + 36 lower-case letters |
| **Refuses** | An AWS key id: `AKIA` or `ASIA` at a token start, exactly sixteen `[0-9A-Z]`, then anything but an ASCII letter or digit (end of text, `-backup`, `_x`) | `AKIA<16>`, `AKIA<16>-ghp_<36>` (both refused) |
| **Refuses** | A private-key block header: `-----BEGIN [A-Z0-9 ]*PRIVATE KEY` — RSA, EC, DSA, OPENSSH, PGP, ENCRYPTED (PKCS#8) or none | `-----BEGIN … PRIVATE KEY` |
| **Refuses** | A **named secret**: an UPPER_SNAKE name of two or more words ending in `_TOKEN`, `_KEY`, `_SECRET` or `_PASSWORD`, **leading underscores included** (`_API_KEY`, `__DB_PASSWORD`, `self._API_KEY`), words joined by one **or more** `_` (`APP__DB_PASSWORD`, `MYAPP__STRIPE__API_KEY`), the first word possibly starting with a digit (`_2FA_SECRET`), and not preceded by an ASCII letter or digit — as a quoted assignment (`NAME = "v"`, `NAME: 'v'`), a `.env` line (`[export ]NAME=v`), a YAML line (`NAME: v`; an optional `- ` list item and an optional quoted name; indented by spaces, tabs or no-break spaces), a line ending in LF, CRLF or any separator above, or a JSON member (`"NAME": "v"`); its literal value — sixteen or more of `[A-Za-z0-9_\-+/=.~\\]`, a JSON `\/` read as `/`, spaces or tabs after an opening quote and before a closing quote ignored — is a **placeholder** (the token rule's test, on the body after any issuer prefix), reported as a fixture, **checked first**; otherwise it is refused when it passes `scan_env.looks_secret` **or is 32 or more hex characters and nothing else** (`credential_shape`'s own pattern; a hex value under `looks_secret`'s entropy 3.4 is refused all the same) | `STRIPE_SECRET_KEY="<40 hex>"`, `_API_KEY = "<32>"` |
| **Refuses** | A quoted value of 20+ `[A-Za-z0-9_\-+/=\\]` (a JSON `\/` read as `/`; spaces or tabs after the opening and before the closing quote ignored) assigned with `=` or `:` to the bare word `api_key`, `api-key`, `apikey`, `secret`, `token`, `passwd` or `password` (case-insensitive; leading underscores included; not preceded by an ASCII letter, digit or `_`; a JSON member keyed by the word, `"api_key": "v"`, included) that is not a slot address and not a **placeholder** (both checked first, as in the named rule), and that passes `scan_env.looks_secret`, or is 32 or more hex characters and nothing else, or whose normalised entropy — on the **whole value**, no prefix stripped — is at least 0.78 (a value led by an issuer prefix needs no second measure: every issuer prefix is a `KNOWN_PREFIX`, so `looks_secret` refuses it) (a hex value longer than 35 characters cannot reach 0.78; the hex rule is what refuses it) | `api_key = "<22 random>"`, `token = "<40 hex>"` |
| **Never refuses** | A **slot address** under a bare word — word segments of letters, either case (joined within a segment by `-`, `_` or `.`, no digits) separated by `/`, then a name as wide as the vault's (`[A-Z_][A-Z0-9_]`: `_` runs anywhere, a digit first only after a leading `_`) of **two or more characters** after its leading underscores and at most 128 in all, leading underscores included, `alpha-web/prod/STRIPE_KEY`, `Alpha-Web/prod/_STRIPE_KEY`, `…/APP__DB_PASSWORD`, `…/_2FA_SECRET`, `…/KEY_` — names a secret and is not judged; a segment with a digit, or a one-character name (`…/X`), makes it a value. A token body under its minimum. A placeholder body — two or more lower-case words of 1–15 letters joined by `-` or `_`, or one repeated character — is reported as a **fixture**, checked before the name rule. A **name** — two or more `_`-separated segments of lower-case letters that is not a placeholder (one word longer than 15 letters) — is skipped, not reported; a one-segment lower-case body is not a name and is refused. After a token that is not judged, the scan resumes right after its prefix, so an inner token is still judged | `sk_your_key_goes_here_please` (fixture), `npm_config_cache_dir` (under minimum) |
| **Never scanned** (`SKIP`) | `tools/check_secrets.py` (it holds the patterns) and `tests/test_secrets.py` (it plants keys to prove they are refused). `ALLOWLIST` names files whose hits are listed as allowed, each with a reason | — |
| **Out of scope** (OBS-94) | An encoded value (`%XX`, `\uXXXX`, `\xXX`, ANSI codes) or an ASCII letter, digit or `_` right before the prefix; a value split by a `\` continuation or by implicit string concatenation; a code-level assignment (`os.environ[...] =`, a Python dict, a Kubernetes `name`/`value` pair, a YAML block scalar, Go `NAME := "v"`, Python `NAME: str = "v"`, TypeScript `const NAME: string = "v"`); a `.env` value followed by `;`; a lower-case name or a bare `KEY`, `_KEY` or `__KEY`; a password that fails `looks_secret`; a dotted value such as a JWT (`eyJ….….…`) under a bare word, whose `.` the bare-word alphabet does not include; a value whose `/`-separated parts before the last are letters only and whose last part is upper-case letters, digits and `_` (`alpha/prod/JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP`, a base32 secret), which reads as a slot address and is not judged; a UTF-16 (or other non-UTF-8 wide) encoded file; files outside the engine folder (`CHANGELOG.md`, `docs/`, root `tools/`, `.github/`) | — |

**Fixture convention.** A test needs a key-shaped value: compose it at run time (`"sk" + "-" + body`), or
make its body a placeholder by the rule above, or add the file to `ALLOWLIST` with a reason. A literal that
matches a refusing row is refused even when it is a vendor's published example (AWS's documentation key
id) or a body of upper-case words such as `FAKE`, `KEY`, `FOR`, `TESTS` joined by `_` after `sk-proj-`:
upper-case words are not a placeholder.

**Public hex values are refused too** (the hex rule's false-positive surface, on purpose): 32 or more hex
characters under a `*_KEY`, `*_TOKEN`, `*_SECRET` or `*_PASSWORD` name, or a bare word, are refused
whatever they hold — a GPG key fingerprint (`SIGNING_KEY=<40 hex>`), a `*_PUBLIC_KEY`, a `CACHE_KEY` that
is a content hash, an `IDEMPOTENCY_KEY`. In a test, compose such a value at run time (`"ab" * 20`, or
`hashlib.sha1(b"…").hexdigest()`), so no literal is in the tree; in shipped code, name it for what it is
(`SIGNING_FINGERPRINT`, `CACHE_DIGEST`), which the named rule does not read; or add the file to
`ALLOWLIST` with the reason.

**What defends the out-of-scope rows** is the known-value leak
scan (`tools/scan_leaks.py`), which looks for the exact values this machine holds wherever they appear,
whatever surrounds them; an encoded copy of a value is PB-157's open gap there.

## Credentials in agent memory

A workflow declares the credentials it needs, by name:

```json
"credentials": [{"project": "alpha-web", "env": "prod", "name": "STRIPE_KEY", "purpose": "charge cards"}]
```

- Each field is an address. A value in any of them is refused, not redacted, because this field
  holds addresses only (`store/workflow.py`, `_credentials`).
- `observatory_checkpoint_latest`, the handoff pack and the acceptance each report every
  declared credential as one of:

  | State | Meaning |
  |---|---|
  | `vault` | the slot exists for that environment; the row carries the `--vault-only` command that uses it |
  | `env-only` | the project holds the name in its own `.env` and not in the vault. It works, and it breaks the rule; the row carries the `vault.py put` command that moves it |
  | `missing` | neither; the row carries the `put` command |
  | `unknown` | the vault directory could not be listed. It is said rather than guessed |

- `credentialsMissing` is true when any credential is `missing` or `env-only`. The next executor
  stops before the step that would fail, instead of failing half-way through it.
- The acceptance reads the vault again rather than trusting the pack. A key put into the vault
  after the pack was made counts.
- A value written into memory by mistake is replaced before it is stored. When it is one of the
  workspace's known values, the marker names its home: `[redacted:vault:PROJECT/ENV/NAME]` for a
  slot, `[redacted:env:PROJECT/NAME]` for an env file. Record that as an exposure with
  `vault.py leak`.

## Checks

| Check | Status |
|---|---|
| `use_secret.py --vault-only` refuses a name held only in a `.env`, and says how to store it | `test_vault_only_refuses_a_project_env_file` |
| a slot is released only under a grant naming it; another slot is `no-grant`, a capability source `credential-source-refused`, a revoked grant `grant-revoked`, another scope `principal-unproven`; the child does not inherit the token; `always` refuses without one; the journal names the verdict, never the token | `test_a_slot_is_released_only_under_a_grant` |
| a credential address carrying a value is refused | `test_a_value_is_never_a_credential_address` |
| a handoff reports `vault`, `env-only`, `missing`; acceptance re-reads the vault; an unreadable vault is `unknown` | `Credentials` cases in `tests/test_workflow_memory.py` |
| a redaction marker names the slot | `test_redaction_names_the_slot_a_value_lives_in` |
| `serve` starts the service from the vault as the same process, refuses a `.env`, and the rotation names it | `test_serve_starts_a_service_from_the_vault_and_rotation_names_it` |
| a finding when an agent's project keeps a secret in its `.env` that the vault does not hold (`agent.secret_outside_vault`) | `test_secrets_outside_the_vault_are_named_with_the_command` |
| a finding when a run for an agent's project took a key from a `.env` (`agent.secret_fallback_used`) | `test_a_run_that_fell_back_to_a_dotenv_is_reported` |
| a finding when a known value was written into agent memory (`secret.seen_in_agent_memory`, critical); the write is stored redacted and journalled by slot name | `test_a_known_value_in_memory_is_journalled_and_reported` |
| the Observatory's own store is in the leak scan: a sighting there means the redactor missed one | `test_the_observatory_store_is_in_the_leak_scan` |
| an agent's note (`observatory_record`) is redacted like a checkpoint | `memory_redact` on the record tool |

## Which projects are agents'

The two findings about a project's keys judge only projects measured as an agent's: a project
a workflow declared a credential for, or one whose local folder carries a Fabric agent manifest
(`fabric-agent.json`). A project an agent merely touched is not judged; guessing would make
every repository an agent's (`tools/agent_secret_findings.py`, `agent_projects`).

## Edge cases

- **Remote services** (a Worker, a Heroku app) cannot read the local vault. The key is issued and
  recorded here, pushed to the platform, and the movement is recorded with
  `vault.py moved --at PROVIDER`. The platform's copy is then known rather than invisible.
- **Rotation and running services.** A value is read when a service starts, so a running
  service keeps the old one until it restarts. `serve` records each consumer by its label and
  `vault.py rotate` names them with the restart command. A service started some other way is
  not on that list.
  `shared_with` in `observatory_credentials` names the other projects holding the same value.
- **The known-value cache** in the redactor reloads when the vault or the env inventory changes.
  A value that has no credential shape and has never been stored anywhere cannot be recognised:
  redaction is a safety net, not a licence to paste.
- **Public code.** Agents published as open source name keys by placeholder (`EXAMPLE_API_KEY`)
  and read them from their environment. Observatory is how the operator's machine fills that
  environment, not a dependency the code must import.

## DigitalOcean rotation evidence

DigitalOcean rotation verifies every selected `--spec-env APP:KEY` literal before any reset.
Its name is not evidence: a plain value must equal the selected database user's current
provider password, or be a database URL with that password, user, engine and the provider's
connection host/port. A `${…}` binding is never rewritten, at any level; `--db` must be one of
the cluster's databases, and `db credential` checks `--db` the same way before it reads a
password (a cluster the provider lists no databases for, and redis/valkey, refuse `--db` with
their own reason). Opaque encrypted values and unverified pool endpoints are refused;
migrate those consumers to App Platform database bindings before rotation. No command in
this flow prints the current password. Only the named consumers are moved, with downtime
between reset and ACTIVE; ACTIVE is not application health verification.

The door writes an owner-only receipt under `store/digitalocean-rotations/` before reset,
initially `reset: unknown`. A lost or malformed response, timeout or nonzero CLI result can
follow a committed reset: exit 5 means **unknown**, not “nothing changed”. Never repeat
`db rotate` to recover it. Use `db credential` to deliver the current value into the selected
vault slot, then reconcile each consumer without another reset. Exit 1 is a refusal before
reset; after an attempt, 2 means deployment unconfirmed, 3 vault delivery failed, 4 an app
update/redeploy or temporary spec cleanup failed, and 6 receipt update failed. The largest
code wins. Independent consumers continue after a confirmed reset despite another consumer's
failure. Provider/vault error bodies are withheld. A crash or failed final receipt write
leaves the earlier uncertain receipt; there is no automatic reconciliation, retry or claim
of a transaction across provider and vault. A failed temporary-file cleanup may leave its
owner-only spec file and is reported for local recovery.

Reset, app updates and redeploys each make one attempt; only read calls retry.
A lost/nonzero mutation response leaves that consumer `unknown`, even if the provider
already applied it. The receipt distinguishes this from `not_started` for a local
pre-call failure. Read current spec and deployment state before any retry; a blind
redeploy is not recovery for a literal. Independent peers still continue. Deployment
confirmation accepts a validated phase equal to `ACTIVE`, never a suffix or arbitrary
provider text; malformed metadata remains unconfirmed and its body is not printed.


Verified by `tests/test_digitalocean_safety.py` and `tests/test_digitalocean_door.py`
using fake provider responses and scratch vaults. Live provider acceptance is separate.
