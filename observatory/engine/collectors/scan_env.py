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
"""
from __future__ import annotations
import hashlib, math, os, pathlib, re, stat, subprocess, sys
from collections import Counter
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import atomic              
import paths              
import safe_git  # noqa: E402  — the engine's one git door
from runtime_identity import IdentityError, load as load_identity

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

#: Shapes that are a credential whatever they are called.
KNOWN_PREFIX = ("sk-", "sk_", "pk_live", "pk_test", "rk_", "ghp_", "gho_", "ghs_",
                "github_pat_", "xoxb-", "xoxp-", "xapp-", "AIza", "AKIA", "ASIA",
                "eyJ", "-----BEGIN", "glpat-", "dop_v1_", "shpss_", "shpat_",
                "SG.", "hf_", "npm_", "dckr_pat_", "figd_", "sntrys_")

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


def wordish(value: str) -> bool:
    """Does this read as a tokenised identifier rather than as randomness?"""
    toks = re.split(r"[-_.]", value)
    if len(toks) < 2:
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


def looks_secret(value: str) -> bool:
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
    if wordish(v):
        return False
    if not re.fullmatch(r"[A-Za-z0-9_\-+/=.]+", v):
        return False
    return entropy(v) >= MIN_SECRET_ENTROPY


def classify(name: str, value: str) -> str:
    """One word per variable, and the value is not one of the four."""
    v = value.strip()
    if not v:
        return "empty"
    if is_placeholder(v):
        return "placeholder"
    if looks_secret(v):
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
    lines = text.splitlines()
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


def env_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Every candidate under the estate, breadth-limited and exclusion-pruned."""
    found: list[pathlib.Path] = []
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = list(os.scandir(d))
        except (PermissionError, OSError):
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if (MAX_DEPTH is None or depth < MAX_DEPTH) \
                            and e.name not in SKIP_DIRS:
                        stack.append((pathlib.Path(e.path), depth + 1))
                elif e.is_file(follow_symlinks=False) and ENV_NAME.match(e.name):
                    found.append(pathlib.Path(e.path))
            except OSError:
                continue
    return sorted(found)


def repo_of(path: pathlib.Path, root: pathlib.Path) -> pathlib.Path | None:
    """The git work tree a file sits in, or None. Walks up, stops at the estate."""
    d = path.parent
    while True:
        if (d / ".git").exists():
            return d
        if d == root or d.parent == d:
            return None
        d = d.parent


def git_states(files: list[pathlib.Path], root: pathlib.Path) -> tuple[dict, list]:
    """`tracked` / `ignored` / `loose` / `no-repo` per file, batched per repository.

    `loose` is the one worth naming: untracked AND unignored is one `git add -A`
    away from a committed secret, and it looks identical to `ignored` in every
    listing that does not ask.
    """
    by_repo: dict[pathlib.Path, list[pathlib.Path]] = {}
    state: dict[str, str] = {}
    degraded: list[dict] = []
    for f in files:
        repo = repo_of(f, root)
        if repo is None:
            state[str(f)] = "no-repo"
        else:
            by_repo.setdefault(repo, []).append(f)

    for repo, group in by_repo.items():
        rel = [str(f.relative_to(repo)) for f in group]
        tracked: set[str] = set()
        ignored: set[str] = set()
        try:
            # Through `safe_git`: `ls-files` here ran the fsmonitor named in the
            # operator's configuration, once per repository per tick.
            p = safe_git.run(["ls-files", "--", *rel], repo=repo, timeout=60)
            tracked = {ln for ln in p.stdout.splitlines() if ln}
            q = safe_git.run(["check-ignore", "--", *rel], repo=repo, timeout=60)
            ignored = {ln for ln in q.stdout.splitlines() if ln}
        except (OSError, subprocess.SubprocessError) as exc:
            degraded.append({"repo": str(repo), "reason": f"{type(exc).__name__}: {exc}",
                             "effect": f"{len(group)} file(s) carry no git state"})
        for f, r in zip(group, rel):
            state[str(f)] = ("tracked" if r in tracked else
                             "ignored" if r in ignored else "loose")
    return state, degraded


def scan(root: pathlib.Path) -> dict:
    pepper = salt()
    files = env_files(root)
    states, degraded = git_states(files, root)
    records = []
    for f in files:
        try:
            st = f.stat()
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            degraded.append({"file": str(f), "reason": f"{type(exc).__name__}: {exc}",
                             "effect": "not read, so its variables are unknown"})
            continue
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
        records.append({
            "path": str(rel),
            "project": rel.parts[0] if rel.parts else "",
            "kind": "template" if f.name.endswith(TEMPLATE_SUFFIXES) else "env",
            "git": states.get(str(f), "no-repo"),
            "mode": oct(stat.S_IMODE(st.st_mode))[2:].rjust(4, "0"),
            "modified_on": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                                   .strftime("%Y-%m-%d"),
            "unparsed_lines": unparsed,
            "variables": variables,
        })
    return {
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "root": str(root),
        "max_depth": MAX_DEPTH,
        # Which salt the fingerprints below were made under, as a public label.
        # A consumer comparing this scan with another checks it first.
        "fingerprint_namespace": namespace(pepper),
        "files": records,
        "degraded": degraded,
    }


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
    try:
        doc = scan(paths.DATA)
    except IdentityError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    atomic.write_json(out, doc)
    n_vars = sum(len(f["variables"]) for f in doc["files"])
    n_secret = sum(1 for f in doc["files"] for v in f["variables"]
                   if v["class"] == "secret")
    loose = sum(1 for f in doc["files"] if f["git"] == "loose")
    tracked = sum(1 for f in doc["files"] if f["git"] == "tracked")
    print(f"env: {len(doc['files'])} file(s), {n_vars} variable(s), "
          f"{n_secret} secret-class, {loose} unignored, {tracked} TRACKED IN GIT")
    for d in doc["degraded"][:5]:
        print(f"  degraded: {d.get('reason')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
