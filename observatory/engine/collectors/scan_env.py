#!/usr/bin/env python3
"""Every .env on this machine, recorded as NAMES and never as values.

WHY. An estate keeps credentials in more places than a vault and a provider
account list: the largest store by far is usually the `.env` files scattered
across project folders, and nothing else here knew they existed. Without this
scan, "does this project already have a given token" is answered by grepping,
and "who else breaks if I rotate this" is not answered at all.

WHAT IT RECORDS, AND WHAT IT REFUSES TO.

  recorded   the file, its project, its mode, when it changed, whether git
             tracks/ignores it, and every variable NAME with a class
  refused    every value, always

CLASSIFICATION NEEDS THE VALUE AND KEEPS NONE OF IT. A name alone cannot tell
a boolean flag from a token merely because both names mention auth, so the value is read, measured
(length, entropy, shape) and dropped. What survives is one word — secret,
config, placeholder or empty.

THE ONE DERIVATION THAT SURVIVES THE VALUE is a salted fingerprint, and it
exists to answer the question curation could not: WHICH PROJECTS HOLD THE SAME
SECRET. `collectors/credential_owners.json` asks an operator to declare that by
hand; two files with one value are the evidence that declaration wants, and it
costs nothing to collect. The salt is local and random, so the
fingerprint is not a lookup against any table that exists off this machine — and
it stays in `store/raw/env.json`, which git ignores. The REGISTRY receives only
the conclusion: this variable is also in that project.

    scan_env.py store/raw/env.json

ONE PASS MAY TAKE SEVERAL TICKS (OBS-220). The walk has no depth limit, so it reads every folder
under the estate root, linked worktrees included: 38 s on a quiet machine, 599 s on one under a load
average above 70 (2026-10-24), which was the whole 40 % of the tick any collector may take. The scan
now reads the step's deadline (`step_budget`): when it comes, the pass stops where it is (the
folders still to walk, the repositories whose git state is read, the files already classified) and
saves that beside the output (`env-scan-state.json`, the same private folder and the same salted
fingerprints, never a value). The next tick resumes it. `env.json` is replaced only by a complete
pass, so every reader keeps the last complete inventory meanwhile; the pass says how many ticks it
took (`pass`). A saved pass under another salt or another root is discarded, never mixed in.
"""
from __future__ import annotations
import hashlib, json, math, os, pathlib, re, stat, subprocess, sys
from collections import Counter
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic              
import paths              
import safe_git  # noqa: E402  — the engine's one git door
import credential_shape  # noqa: E402  — the one list of issuer prefixes
from runtime_identity import IdentityError, load as load_identity
import step_budget  # noqa: E402  (the time this step has left, OBS-220)

#: NO DEPTH LIMIT. A fixed depth looks sufficient and silently misses files a
#: level or two deeper than expected, which is the one failure this repository
#: refuses by name (`tests/test_no_silent_truncation.py`). The bound that
#: remains is `SKIP_DIRS`, and that one is a statement about ownership rather
#: than about cost: a dependency's `.env` is not this estate's secret. Removing
#: the depth limit measurably costs little.
MAX_DEPTH = None

#: Directories that hold OTHER people's `.env` files. A dependency's fixture is
#: not this estate's secret, and `node_modules` alone would multiply the scan by
#: two orders of magnitude.
SKIP_DIRS = {
    "node_modules", ".git", "vendor", "venv", ".venv", "env", ".env.d",
    "site-packages", "dist", "build", ".next", ".nuxt", "__pycache__",
    ".terraform", "Pods", ".gradle", "target", ".tox", ".mypy_cache",
    ".pytest_cache", "coverage", ".cache", "tmp", ".idea", ".vscode",
}

#: A template declares what a project NEEDS; it holds no secret by construction.
#: Recorded rather than skipped, because "this project wants a payment secret
#: key and has none" is exactly the question the reuse view answers.
TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")

ENV_NAME = re.compile(r"^\.env(\..+)?$|^\.envrc$")

#: `KEY=value`, with or without `export`, and with an optional inline comment
#: that only counts outside quotes.
ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

#: A comment at the start of the value, or after whitespace inside it. Anchored
#: at the start too: `KEY=  # note` leaves the note flush left once stripped.
COMMENT = re.compile(r"(^|\s)#.*$")

#: A NAME that is itself secret-shaped. Rare, and the product of a malformed
#: file rather than a convention — but this output is committed, so a name that
#: looks like a value is redacted rather than published.
SECRET_SHAPED = re.compile(r"(sk|pk|rk|ghp|gho|ghs|xoxb|xoxp|AKIA|AIza)[-_A-Za-z0-9]{16,}")

SECRET_NAME = re.compile(
    r"(SECRET|TOKEN|PASSW|API_?KEY|ACCESS_?KEY|PRIVATE|CREDENTIAL|"
    r"_DSN$|DATABASE_URL|CONN(ECTION)?_?STR|CLIENT_?SECRET|SIGNING|"
    r"_SALT$|SESSION|COOKIE|WEBHOOK|_PAT$|_KEY$|^KEY$|BEARER|AUTH)", re.I)

#: Values that are obviously not live. A project full of these is a project
#: waiting for credentials, which the reuse view reads as a question rather than
#: as an inventory.
#:
#: TWO PATTERNS, because the first version was one and anchored at both ends —
#: so `your-gemini-api-key` did not match `^your[-_ ]?$` and was then classified
#: `secret` by its NAME. That is not a rounding error: it reported 152 committed
#: `.env.example` templates as files carrying live credentials, which is the
#: shape of an alarm nobody can act on. A lead-in is a prefix; an exact word is
#: exact.
PLACEHOLDER_LEAD = re.compile(
    r"^(your|my|the|some|todo|tbd|changeme|change[-_]me|replace|example|dummy|"
    r"sample|placeholder|insert|fill|enter|add|put|paste|abc123|foo|bar|any)"
    r"([-_. ]|$)", re.I)
PLACEHOLDER_EXACT = re.compile(
    r"^(none|null|nil|n/?a|undefined|x{3,}|\*{3,}|\.{3,}|-+|<.*>|\{\{.*\}\}|"
    r"\$\{.*\}|__.*__|%.*%)$", re.I)
#: A value that ANNOUNCES itself as a slot to fill, wherever the words sit.
PLACEHOLDER_MARK = re.compile(
    r"(_?here$|[-_]here$|goes[-_]here|your[-_]|xxxxx|<[^>]+>|\.\.\.)", re.I)

#: Shapes that are a credential whatever they are called. ONE LIST (review
#: round 3): every issuer prefix the redactor knows (`credential_shape.PREFIXES`,
#: which the commit gate reads too), plus the shapes that are not issuer
#: prefixes — a JWT, a PEM block, an AWS id, SendGrid's `SG.`.
KNOWN_PREFIX = tuple(dict.fromkeys((*credential_shape.PREFIXES, "pk_live", "pk_test", "AKIA", "ASIA",
                                    "eyJ", "-----BEGIN", "SG.")))

#: A URL carrying its own credentials — `postgres://user:pass@host`. The name may
#: be anything; the shape decides.
URL_WITH_AUTH = re.compile(r"^[a-z][a-z0-9+.-]*://[^/\s:@]+:([^/\s@]+)@", re.I)

#: Passwords that are the word for a password. Not a blocklist for people — a
#: list of what an example file says where a secret would go.
EXAMPLE_PASSWORDS = {"password", "passwd", "pass", "secret", "postgres", "mysql",
                     "root", "admin", "test", "dev", "user", "username", "db",
                     "localhost", "example", "changeme", "hunter2", "12345",
                     "123456", "pwd", "mypassword", "yourpassword"}

#: A tokenised identifier — `gemini-2.5-flash-preview`, `us-east-1`,
#: `postgres-primary`. Long, and its entropy is genuinely above 3.5, which is why
#: an entropy-only `looks_secret` would call it a credential. Randomness is not
#: length: a key has no word boundaries.
#:
#: SEPARATORS ALONE ARE NOT THE TEST: a dash-separated UUID is a real API key
#: shape and has four separators. What distinguishes an identifier is a WORD — a run of letters
#: that is not also hex.
HEX_ONLY = re.compile(r"^[0-9a-fA-F]+$")

#: An endpoint carrying no credentials is configuration, however long it is.
URL_NO_AUTH = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)

PLAIN = re.compile(r"^(true|false|yes|no|on|off|none|null|\d+(\.\d+)?|"
                   r"localhost(:\d+)?|127\.0\.0\.1(:\d+)?|0\.0\.0\.0(:\d+)?|"
                   r"development|production|staging|test|debug|info|warn|error)$", re.I)

SALT_FILE = paths.STATE / ".env-fingerprint-salt"

#: The salt behind every fingerprint: random, local, owner-only, and never
#: written anywhere a scan result goes. `FINGERPRINT_VERSION` names the scheme,
#: so fingerprints made under a different scheme are never compared as equal.
FINGERPRINT_VERSION = "fp1"


def salt() -> str:
    """The fingerprint salt, read from its owner-only state file; a read never creates one."""
    return load_identity(SALT_FILE, 'env-fingerprint-salt')


def namespace(pepper: str) -> str:
    """A public label for the salt, so two scans can tell whether they are comparable.

    Fingerprints are only equal across scans made under the same salt. The
    label is a one-way hash of the scheme version and the salt, so it says
    "same salt" or "different salt" without revealing it — the salt itself is
    what keeps a fingerprint from being a lookup against a precomputed table.
    Consumers withhold a cross-scan comparison when the labels differ.
    """
    digest = hashlib.sha256(
        (f"observatory-fingerprint-namespace\x00{FINGERPRINT_VERSION}\x00"
         + pepper).encode("utf-8")).hexdigest()[:16]
    return f"{FINGERPRINT_VERSION}:{digest}"


#: An issuer prefix and a random tail: `ntn_<46>`, `ol_api_<38>`, `secret_<40>`.
#: The prefix is at most eight characters, the tail one run of twenty or more
#: with no separator in it.
PREFIXED_TAIL = re.compile(r"^([A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*)[-_.]([A-Za-z0-9]{20,})$")

#: A canonical UUID. It has the entropy of a key, so its NAME decides (INB-002).
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

#: Names that say "this value identifies something": an id, a price, a
#: release, an image tag, a collection, a parent page, a project, an
#: organisation, a bucket. Such a value can carry a prefix in front of a random
#: run — `price_…`, `org-…`, `prj_…`, `sha-<hex>` — exactly like an issued
#: token, and is public by design. Under `SECRET_NAME` as well (`*_SECRET_ID`,
#: `*_TOKEN_ID`) the secret rule wins: the safe default.
IDENTIFIER_NAME = re.compile(r"(_ID$|_PRICE|_RELEASE$|_TAG$|_COLLECTION$|_PARENT$|_PROJECT$|"
                             r"_ORG$|_BUCKET$)", re.I)


def _random_tail(value: str) -> bool:
    """Is this a short prefix in front of a random run, the shape of an issued token?

    A PREFIX IS NOT A WORD (INB-002). `wordish` read `ntn` and `api` as words,
    so a Notion or Outline token — a random 46- or 38-character run behind its
    issuer's prefix — was filed as configuration. The tail must mix letters and
    digits as well: a long run of letters alone is a name, not a key.
    """
    m = PREFIXED_TAIL.match(value)
    if not m or len(m.group(1)) > 8:
        return False
    tail = m.group(2)
    return (any(c.isdigit() for c in tail) and any(c.isalpha() for c in tail)
            and entropy(tail) >= MIN_SECRET_ENTROPY)


def wordish(value: str, *, tail_tolerant: bool = True) -> bool:
    """Does this read as a tokenised identifier rather than as randomness?

    `tail_tolerant=False` is the reading before INB-002, in which a prefix is
    a word; `classify` asks it to tell a value that is random throughout from
    one whose only randomness is its tail.
    """
    toks = re.split(r"[-_.]", value)
    if len(toks) < 2:
        return False
    if tail_tolerant and _random_tail(value):
        return False
    return any(len(t) >= 3 and t.isalpha() and not HEX_ONLY.match(t) for t in toks)


def entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def is_placeholder(value: str) -> bool:
    v = value.strip()
    return bool(PLACEHOLDER_LEAD.match(v) or PLACEHOLDER_EXACT.match(v)
                or PLACEHOLDER_MARK.search(v))


#: Below this a value is not decided by shape alone — the name still can.
MIN_SECRET_LEN = 24
MIN_SECRET_ENTROPY = 3.4


def looks_secret(value: str, *, tail_tolerant: bool = True) -> bool:
    """The value's own shape, independent of what it is called.

    Order matters. A known issuer prefix is decisive whatever else is true; a URL
    carrying `user:pass@` is a credential and one without is an address; and only
    then does the entropy test run, against a value that has already failed to be
    a tokenised identifier.
    """
    v = value.strip()
    if v.startswith(KNOWN_PREFIX):
        return True
    pw = URL_WITH_AUTH.match(v)
    if pw:
        # `postgres://user:password@localhost/db` is a template, not a leak. The
        # shape says credential and the credential says "fill me in"; 58 committed
        # examples were reported as live databases before this line existed.
        return not is_placeholder(pw.group(1)) and pw.group(1).lower() not in EXAMPLE_PASSWORDS
    if URL_NO_AUTH.match(v):
        return False
    if len(v) < MIN_SECRET_LEN:
        return False
    if wordish(v, tail_tolerant=tail_tolerant):
        return False
    if not re.fullmatch(r"[A-Za-z0-9_\-+/=.]+", v):
        return False
    return entropy(v) >= MIN_SECRET_ENTROPY


def _random_run(value: str) -> bool:
    """Random once its separators are removed: 20+ characters of a key's alphabet,
    a digit or both cases, and a key's entropy. A URL, a sentence or a short word
    is not."""
    if not re.fullmatch(r"[A-Za-z0-9_\-.+/=]+", value):
        return False
    bare = re.sub(r"[-_.]", "", value)
    mixed = any(c.isdigit() for c in bare) or (any(c.isupper() for c in bare) and any(c.islower() for c in bare))
    return len(bare) >= 20 and mixed and entropy(bare) >= MIN_SECRET_ENTROPY


def classify(name: str, value: str) -> str:
    """One word per variable, and the value is not one of the four."""
    v = value.strip()
    if not v:
        return "empty"
    if is_placeholder(v):
        return "placeholder"
    secret_name = bool(SECRET_NAME.search(name))
    identifier_name = bool(IDENTIFIER_NAME.search(name))
    if UUID.match(v):
        # A UUID IS DECIDED BY ITS NAME (INB-002): an Outline collection id read
        # as a secret on entropy alone, while a UUID-shaped API key is real. The
        # secret rule wins a name both claim (`VAULT_SECRET_ID`); a name neither
        # claims falls through to the shape, so the nil UUID is configuration.
        if secret_name:
            return "secret"
        if identifier_name:
            return "config"
    if secret_name and _random_run(v):
        # A SECRET'S NAME WINS OVER WORD SHAPE (review round 3). A random body
        # that happens to hold `-` or `_` — Google's `GOCSPX-…`, Tavily's
        # `tvly-…`, any URL-safe base64 — split into "words", and under
        # GOOGLE_CLIENT_SECRET it was a secret less than half the time.
        return "secret"
    if looks_secret(v):
        # AN IDENTIFIER'S PREFIX IS NOT AN ISSUER'S (INB-002, review round 1).
        # When the random tail is the ONLY reason to call the value secret — the
        # reading before INB-002 said words — and the name says identifier, it
        # is a price, an organisation, a tag: configuration. A name the secret
        # rule claims keeps it secret, and so does a known issuer prefix:
        # `looks_secret(v, tail_tolerant=False)` is True for one, so the last
        # test below already holds it.
        if identifier_name and not secret_name and not looks_secret(v, tail_tolerant=False):
            return "config"
        return "secret"
    if PLAIN.match(v) or URL_NO_AUTH.match(v):
        # AN ADDRESS IS NOT A CREDENTIAL, whatever it is called. A redirect
        # URL named `..._OAUTH_REDIRECT_URL` would otherwise reach the name rule
        # below on the `AUTH` inside `OAUTH` and be filed as a secret.
        return "config"
    # THE NAME DECIDES LAST, and only about a value whose own shape said nothing.
    # A placeholder value under a name ending in `_KEY` matches `_KEY$`; a name
    # cannot outvote a value that reads as a word.
    if SECRET_NAME.search(name) and len(v) >= 8 and not wordish(v):
        return "secret"
    return "config"


def parse(text: str) -> tuple[list[tuple[str, str]], int]:
    """`KEY=value` pairs in file order, plus the count of lines nothing matched.

    Quoted values may span lines — a PEM private key in a `.env` is one variable,
    not forty unparsable lines, and counting it as the latter would report a
    clean file as damaged.
    """
    out: list[tuple[str, str]] = []
    unparsed = 0
    # A byte-order mark is not part of the first name (review round 7: the first
    # variable of a `.env` saved with one was never inventoried).
    lines = (text[1:] if text.startswith("﻿") else text).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = ASSIGN.match(line)
        if not m:
            unparsed += 1
            continue
        name, rest = m.group(1), m.group(2)
        rest = rest.lstrip()
        if rest[:1] in ('"', "'"):
            quote = rest[0]
            body = rest[1:]
            while quote not in body and i < len(lines):
                body += "\n" + lines[i]
                i += 1
            value = body.split(quote, 1)[0]
        else:
            # AN INLINE COMMENT IS NOT A VALUE. `KEY=  # what it is for` would
            # leave the comment as the value, and an English sentence of that
            # length has the entropy of a credential — so committed templates
            # would be reported as holding live keys.
            value = COMMENT.sub("", rest).strip()
        out.append((name, value))
    return out, unparsed


def repo_of(path: pathlib.Path, root: pathlib.Path) -> pathlib.Path | None:
    """The git work tree a file sits in, or None. Walks up, stops at the estate."""
    d = path.parent
    while True:
        if (d / ".git").exists():
            return d
        if d == root or d.parent == d:
            return None
        d = d.parent


#: Seconds kept before the step's deadline to save where the pass stopped (OBS-220).
MARGIN_SECONDS = 10
#: The shape of a saved pass, and the file beside the output it is saved in.
STATE_SCHEMA = 1
STATE_FILE = "env-scan-state.json"
#: The longest a single git read may take, as before; less when the deadline is nearer.
GIT_TIMEOUT = 60


def _out_of_time() -> bool:
    return step_budget.remaining() < MARGIN_SECONDS


def fresh_pass(root: pathlib.Path, label: str) -> dict:
    """A pass that has not started: the root to walk and nothing found yet."""
    return {"schema": STATE_SCHEMA, "root": str(root), "fingerprint_namespace": label,
            "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "ticks": 0,
            "stack": [[str(root), 0]], "found": [], "walk_done": False,
            "states": {}, "repos_done": [], "records": [], "recorded": [], "degraded": []}


def usable(saved, root: pathlib.Path, label: str) -> bool:
    """A saved pass is resumed only under the same schema, root and salt."""
    return (isinstance(saved, dict) and saved.get("schema") == STATE_SCHEMA
            and saved.get("root") == str(root) and saved.get("fingerprint_namespace") == label
            and isinstance(saved.get("stack"), list) and isinstance(saved.get("found"), list)
            and isinstance(saved.get("states"), dict) and isinstance(saved.get("repos_done"), list)
            and isinstance(saved.get("records"), list) and isinstance(saved.get("recorded"), list)
            and isinstance(saved.get("degraded"), list))


def _walk(p: dict) -> bool:
    """Continue the walk of `p` over every candidate under the estate, exclusion-pruned (SKIP_DIRS);
    False, with the folders still to walk saved in `p`, when the deadline came first."""
    stack = [(pathlib.Path(d), int(depth)) for d, depth in p["stack"]]
    found = list(p["found"])
    while stack:
        if _out_of_time():
            p["stack"] = [[str(d), depth] for d, depth in stack]
            p["found"] = found
            return False
        d, depth = stack.pop()
        try:
            entries = list(os.scandir(d))
        except (PermissionError, OSError):
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if (MAX_DEPTH is None or depth < MAX_DEPTH) and e.name not in SKIP_DIRS:
                        stack.append((pathlib.Path(e.path), depth + 1))
                elif e.is_file(follow_symlinks=False) and ENV_NAME.match(e.name):
                    found.append(e.path)
            except OSError:
                continue
    p["stack"], p["found"], p["walk_done"] = [], sorted(set(found)), True
    return True


def _git(p: dict, root: pathlib.Path) -> bool:
    """`tracked` / `ignored` / `loose` / `no-repo` per found file, batched per repository and read one
    repository at a time, so a pass resumes between them; False when the deadline came first.

    `loose` is the one worth naming: untracked AND unignored is one `git add -A`
    away from a committed secret, and it looks identical to `ignored` in every
    listing that does not ask."""
    by_repo: dict[pathlib.Path, list[pathlib.Path]] = {}
    for name in p["found"]:
        f = pathlib.Path(name)
        repo = repo_of(f, root)
        if repo is None:
            p["states"][name] = "no-repo"
        else:
            by_repo.setdefault(repo, []).append(f)
    done = set(p["repos_done"])
    for repo in sorted(by_repo):
        if str(repo) in done:
            continue
        if _out_of_time():
            return False
        group = by_repo[repo]
        rel = [str(f.relative_to(repo)) for f in group]
        tracked: set[str] = set()
        ignored: set[str] = set()
        timeout = max(1.0, min(float(GIT_TIMEOUT), step_budget.remaining() - MARGIN_SECONDS))
        try:
            # Through `safe_git`: `ls-files` here ran the fsmonitor named in the
            # operator's configuration, once per repository per tick.
            q = safe_git.run(["ls-files", "--", *rel], repo=repo, timeout=timeout)
            tracked = {ln for ln in q.stdout.splitlines() if ln}
            q = safe_git.run(["check-ignore", "--", *rel], repo=repo, timeout=timeout)
            ignored = {ln for ln in q.stdout.splitlines() if ln}
        except (OSError, subprocess.SubprocessError) as exc:
            if _out_of_time():
                return False            # the deadline, not the repository: read it again next tick
            p["degraded"].append({"repo": str(repo), "reason": f"{type(exc).__name__}: {exc}",
                                  "effect": f"{len(group)} file(s) carry no git state"})
        for f, r in zip(group, rel):
            p["states"][str(f)] = ("tracked" if r in tracked else "ignored" if r in ignored else "loose")
        p["repos_done"].append(str(repo))
    p["git_done"] = True
    return True


def git_states(files: list[pathlib.Path], root: pathlib.Path) -> tuple[dict, list]:
    """`tracked` / `ignored` / `loose` / `no-repo` per file and the degradations, in one call (`_git`
    over a pass of these files). Outside a tick there is no deadline and every repository is read."""
    p = {"found": [str(f) for f in files], "states": {}, "repos_done": [], "degraded": []}
    _git(p, root)
    return p["states"], p["degraded"]


def _records(p: dict, root: pathlib.Path, pepper: str) -> bool:
    """Classify every found file that is not classified yet; False when the deadline came first."""
    recorded = set(p["recorded"])
    for name in p["found"]:
        if name in recorded:
            continue
        if _out_of_time():
            return False
        p["recorded"].append(name)
        record = _record(pathlib.Path(name), root, pepper, p["states"], p["degraded"])
        if record is not None:
            p["records"].append(record)
    return True


def scan_pass(root: pathlib.Path, saved: dict | None = None) -> tuple[dict | None, dict]:
    """(the document, the pass): the document when the pass is complete, else None and the pass to
    save, resumed by the next call. Outside a tick there is no deadline and one call completes it."""
    pepper = salt()
    label = namespace(pepper)
    p = saved if usable(saved, root, label) else fresh_pass(root, label)
    p["ticks"] = int(p.get("ticks") or 0) + 1
    if not (p["walk_done"] or _walk(p)) or not _git(p, root) or not _records(p, root, pepper):
        return None, p
    return {
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root": str(root),
        "max_depth": MAX_DEPTH,
        # Which salt the fingerprints below were made under, as a public label.
        # A consumer comparing this scan with another checks it first.
        "fingerprint_namespace": label,
        "files": sorted(p["records"], key=lambda r: r["path"]),
        "degraded": p["degraded"],
        # How the pass went: when it began and how many ticks it took (OBS-220).
        "pass": {"started_at": p["started_at"], "ticks": p["ticks"]},
    }, p


def _record(f: pathlib.Path, root: pathlib.Path, pepper: str, states: dict, degraded: list) -> dict | None:
    """One file's record: names, classes and, for a secret, a salted fingerprint; never a value."""
    try:
        st = f.stat()
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        degraded.append({"file": str(f), "reason": f"{type(exc).__name__}: {exc}",
                         "effect": "not read, so its variables are unknown"})
        return None
    pairs, unparsed = parse(text)
    rel = f.relative_to(root)
    variables = []
    for name, value in pairs:
        cls = classify(name, value)
        shown = name if not SECRET_SHAPED.search(name) else "<redacted: the NAME is secret-shaped>"
        row = {"name": shown, "class": cls}
        if cls == "secret":
            # SECRETS ONLY. The first version fingerprinted `config` too, and
            # the result was a "shared value" list whose widest row was forty
            # projects agreeing that `APP_ENV` is `production`. Two projects
            # holding the same configuration is a coincidence of vocabulary;
            # two holding the same credential is a rotation that breaks both.
            row["fingerprint"] = hashlib.sha256(
                (pepper + value).encode("utf-8")).hexdigest()[:16]
        variables.append(row)
    return {
        "path": str(rel),
        "project": rel.parts[0] if rel.parts else "",
        "kind": "template" if f.name.endswith(TEMPLATE_SUFFIXES) else "env",
        "git": states.get(str(f), "no-repo"),
        "mode": oct(stat.S_IMODE(st.st_mode))[2:].rjust(4, "0"),
        "modified_on": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                               .strftime("%Y-%m-%d"),
        "unparsed_lines": unparsed,
        "variables": variables,
    }


def scan(root: pathlib.Path) -> dict | None:
    """One whole pass; None only when a step deadline stops it (`scan_pass` saves and resumes)."""
    return scan_pass(root)[0]


def main(argv: list[str]) -> int:
    out = pathlib.Path(argv[1]) if len(argv) > 1 else paths.SCRATCH / "env.json"
    if not paths.DATA.is_dir():
        atomic.write_json(out, {
            "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "root": str(paths.DATA), "files": [],
            "degraded": [{"reason": f"{paths.DATA} does not exist here",
                          "effect": "no env file is known, which is not the same "
                                    "as none existing"}]})
        print(f"env: {paths.DATA} does not exist — nothing scanned")
        return 0
    state = out.with_name(STATE_FILE)
    try:
        saved = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = None
    try:
        doc, progress = scan_pass(paths.DATA, saved)
    except IdentityError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if doc is None:
        # THE DEADLINE CAME FIRST: the pass is saved and the next tick resumes it. `env.json` stays
        # the last complete pass; a reader is never handed half an inventory.
        atomic.write_json(state, progress)
        left = ("the walk" if not progress["walk_done"] else
                "the git state of the files found" if not progress.get("git_done") else "the files found")
        print(f"env: the step's time ran out during {left} (tick {progress['ticks']} of this pass, "
              f"{len(progress['found'])} file(s) found so far); the pass resumes on the next tick and "
              f"env.json stays the last complete one")
        return 0
    atomic.write_json(out, doc)
    state.unlink(missing_ok=True)
    n_vars = sum(len(f["variables"]) for f in doc["files"])
    n_secret = sum(1 for f in doc["files"] for v in f["variables"]
                   if v["class"] == "secret")
    loose = sum(1 for f in doc["files"] if f["git"] == "loose")
    tracked = sum(1 for f in doc["files"] if f["git"] == "tracked")
    over = f", over {doc['pass']['ticks']} ticks" if doc["pass"]["ticks"] > 1 else ""
    print(f"env: {len(doc['files'])} file(s), {n_vars} variable(s), "
          f"{n_secret} secret-class, {loose} unignored, {tracked} TRACKED IN GIT{over}")
    for d in doc["degraded"][:5]:
        print(f"  degraded: {d.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
