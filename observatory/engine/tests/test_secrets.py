#!/usr/bin/env python3
"""The claim "no key is ever committed", as an exit code — and the false critical.

The engine says a great deal about key handling: one file per key at mode 600,
a loose-permission file refused rather than used, `key_status` printing a shape
and never a value, the scheduler unit carrying nothing. **Nothing verified that
a key had never been committed.** The one assertion that came close ran
`git grep -lE "sk-or-v1-[A-Za-z0-9]{20,}" HEAD` under the name *"no real key
shape appears anywhere in history"*. Two things wrong with it:

* `git grep … HEAD` searches the HEAD TREE. The name claimed history.
* The pattern was one vendor wide and excluded hyphens, so it would have missed
  its own fixture and every GitHub, AWS, Slack or Anthropic token.

**The audit's two CRITICAL findings were false, and the evidence is the reason
to say so rather than the variable name.** A key-provider suite held a literal
fixture key whose body was a readable English phrase:

    that body                3.65 bits/char after the vendor prefix
    a random 32-char key     4.60
    a random 48-char key     5.03
    'placeholder' x4         3.10

So the floor is 0.78 of what a body that length could carry — NORMALISED,
because the first version used raw bits per character and a real AWS key sailed
through: its body is exactly sixteen characters, and the maximum entropy of
sixteen characters is log2(16) = 4.0, below the 4.3 floor. The floor was above
the ceiling.

And the deeper result, which changed the design: over 4000 draws a real
sixteen-character AWS body's normalised entropy has a minimum of 0.725 and a
first percentile of 0.789 — OVERLAPPING the fixture at 0.72 and the placeholder
at 0.74. Sixteen characters do not hold the information to separate them, so a
structurally exact vendor shape is decided by its prefix and never measured.

**Entropy decides where it can, and the name never does**: a real key called
`FAKE_KEY` must still fail, which is why a self-describing word lowers nothing
on its own — driven below.

Every key-shaped fixture here is COMPOSED at runtime, so the tree holds no
key-shaped literal at all. An allowlist entry would have taught only this
scanner; removing the shape convinces every scanner, including the ones nobody
here configures. A permanent false critical is what teaches an operator to skip
the one that is real.

The tree scan needs `git ls-files`, and a portable sandbox is not a checkout,
so the tree-level assertions commit a COPY of the engine source into a
throwaway repository and scan that: the claim is about what ships, and the copy
is byte for byte what ships.
"""
from __future__ import annotations
import json, os, pathlib, secrets, shutil, string, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
FAILURES: list[str] = []
ALPHA = string.ascii_letters + string.digits

#: Vendor prefixes and the key-block header, COMPOSED: a literal prefix in
#: front of a body is exactly what a third-party scanner — and this
#: repository's release check — reads as a leaked key.
SK = "sk" + "-"
OR = SK + "or-v1-"
KEY_HEADER = "-----BEGIN " + "RSA PRIVATE" + " KEY-----"
#: The phrase-bodied fixture the audit mistook for a credential, rebuilt.
PHRASE_FIXTURE = OR + "a-fake-key-" + "used-only-by-this-test"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def random_body(n: int) -> str:
    return "".join(secrets.choice(ALPHA) for _ in range(n))


def scan_string(text: str, where: str = "planted.py") -> dict:
    """Run the checker's own scan over a string, in process.

    The tree scan needs `git ls-files`, so planting a file would mean committing
    it — and a test that commits a credential to prove a check works has done
    the thing the check exists to prevent.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import check_secrets
    hits = check_secrets.scan_text(text, where)
    return {"credentials": [h for h in hits
                            if h.get("credential") or h["entropy"] is None],
            "fixtures": [h for h in hits
                         if not (h.get("credential") or h["entropy"] is None)]}


_SHIPPED: pathlib.Path | None = None


def shipped_checkout() -> pathlib.Path:
    """A throwaway git repository holding a copy of the engine source.

    One commit, made with a fixed identity and no user or system git config, so
    the scanner's `git ls-files` and `git log -p` see exactly the shipped files.
    """
    global _SHIPPED
    if _SHIPPED is not None:
        return _SHIPPED
    work = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sec-")) / "engine"
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", "*.pyc"))
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "-c", "commit.gpgsign=false", "commit", "-qm", "shipped tree")):
        subprocess.run(["git", *args], cwd=work, env=env, check=True,
                       capture_output=True, timeout=120)
    _SHIPPED = work
    return work


def run_checker(*args: str) -> subprocess.CompletedProcess:
    work = shipped_checkout()
    return subprocess.run([PY, "tools/check_secrets.py", *args], cwd=work,
                          capture_output=True, text=True, timeout=600,
                          env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                               "GIT_CONFIG_GLOBAL": os.devnull})


# ─────────── a real credential is refused ──────────────────────────────

def test_every_vendor_shape_is_caught() -> None:
    planted = {
        "openai/openrouter": f'KEY = "{SK}{random_body(48)}"',
        "openrouter with its prefix": f'KEY = "{OR}{random_body(64)}"',
        "github token": f'T = "ghp_{random_body(36)}"',
        "github fine-grained": f'T = "github_pat_{random_body(40)}"',
        "aws access key": 'K = "AKIA' + "".join(
            secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16)) + '"',
        "linear api key": f'K = "lin_api_{random_body(40)}"',
        "slack token": f'K = "xoxb-{random_body(40)}"',
        "google api key": f'K = "AIza{random_body(35)}"',
        "private key block": KEY_HEADER + "\nMII...\n",
        "an unbranded named secret": f'api_key = "{random_body(40)}"',
        "notion token": f'K = "ntn_{random_body(46)}"',
        "outline token": f'K = "ol_api_{random_body(38)}"',
    }
    for label, text in planted.items():
        got = scan_string(text)
        check(f"a planted {label} is refused", bool(got["credentials"]),
              f"{got} — a scanner that misses this is a green nobody earned")


def test_every_prefix_the_redactor_knows_is_caught() -> None:
    """One list of issuer prefixes (review round 2): `credential_shape.PREFIXES`
    redacts them, and the commit gate must refuse each of them too."""
    import credential_shape
    for pre in credential_shape.PREFIXES:
        got = scan_string(f'K = "{pre}{random_body(40)}"')
        check(f"a planted {pre}… token is refused", bool(got["credentials"]), str(got)[:200])
    check("ntn_ and ol_api_ are among them", {"ntn_", "ol_api_"} <= set(credential_shape.PREFIXES))


def test_a_short_vendor_body_and_a_named_token_are_refused() -> None:
    """Review round 3: a 20-character `glpat-` body was called a fixture, and
    NOTION_TOKEN="secret_…" passed the named-secret rule (`\\btoken\\b`)."""
    got = scan_string(f'K = "glpat-{random_body(20)}"')
    check("a 20-character glpat- body is refused", bool(got["credentials"]), str(got)[:200])
    got = scan_string(f'NOTION_TOKEN = "secret_{random_body(43)}"')
    check("a *_TOKEN name with a secret_ value is refused", bool(got["credentials"]), str(got)[:200])
    got = scan_string(f'STRIPE_WEBHOOK_KEY = "{random_body(40)}"')
    check("a *_KEY name with an unbranded value is refused", bool(got["credentials"]), str(got)[:200])


def test_a_known_prefix_with_its_body_is_always_refused() -> None:
    """The gate's guarantee (round 4): a known issuer prefix and a body at or above
    its minimum is refused, whatever its entropy — hex, a long run, after a literal \\n."""
    import random
    rng = random.Random(4)
    hexb = lambda n: "".join(rng.choices("0123456789abcdef", k=n))
    alnum = lambda n: "".join(rng.choices(string.ascii_letters + string.digits, k=n))
    planted = {
        "sk-or-v1- + 64 hex": f'K = "{OR}{hexb(64)}"',
        "dop_v1_ + 64 hex": f'K = "dop_v1_{hexb(64)}"',
        "sntrys_ + base64": f'K = "sntrys_{alnum(120)}"',
        "long sk-proj-": f'K = "{SK}proj-{alnum(160)}"',
        "prefix + 200 alnum": f'K = "ghp_{alnum(200)}"',
        "after a literal backslash-n": 'K = "line one\\n' + f'ghp_{alnum(36)}"',
    }
    for label, text in planted.items():
        got = scan_string(text)
        check(f"{label} is refused", bool(got["credentials"]), str(got)[:200])


def test_named_secrets_in_all_four_forms() -> None:
    """UPPER_SNAKE names ending in _TOKEN/_KEY/_SECRET/_PASSWORD, value of secret shape (round 4)."""
    import random
    rng = random.Random(5)
    hex40 = "".join(rng.choices("0123456789abcdef", k=40))
    hex64 = "".join(rng.choices("0123456789abcdef", k=64))
    forms = {
        "quoted assignment": f'APP_SECRET = "{hex64}"',
        "unquoted .env": f"APP_SECRET={hex40}",
        "YAML": f"SERVICE_TOKEN: {hex64}",
        "JSON": f'{{"DEPLOY_KEY": "{hex40}"}}',
        "export .env": f"export DB_PASSWORD={hex40}",
    }
    for label, text in forms.items():
        got = scan_string(text, "planted.env")
        check(f"a named secret as {label} is refused", bool(got["credentials"]), str(got)[:200])
    for label, text in {"lower-case name": f'app_secret = "{hex64}"'.replace("app_secret", "app_hash"),
                        "a bare KEY": f"KEY={hex40}",
                        "a word value": "APP_SECRET=changeme-in-production-please"}.items():
        got = scan_string(text, "planted.env")
        check(f"{label} is out of scope", not got["credentials"], str(got)[:200])


def test_a_word_before_a_prefix_is_not_an_issuer() -> None:
    """An identifier that merely contains a prefix is a name, not a key (review round 3).

    Round 5: a snake-case name is skipped only when EVERY segment is lower-case
    letters; an `npm_` name with digit segments (`…_version_2024_10_12_build`) is
    refused by the grammar (deliberately: a prefix and a body of its minimum
    length), so that case is composed at run time below."""
    for text in ('model = load_hf_AutoModelForCausalLM2024abc()',
                 'def test_sk_live_rejects_a_bad_signature_2024(): pass'):
        got = scan_string(text)
        check(f"{text[:30]}… is not refused", not got["credentials"] and not got["fixtures"], str(got)[:200])
    # Round 6: lower-case words after a prefix are a placeholder, REPORTED as a fixture.
    got = scan_string('v = "npm_package_version_build_name"')
    check("npm_ + lower-case words is a fixture, not silence", not got["credentials"] and bool(got["fixtures"]),
          str(got)[:200])
    got = scan_string('v = "' + "npm" + "_package_version_2024_10_12_build" + '"')
    check("a prefix with a digit-segment snake body is refused by the grammar", bool(got["credentials"]),
          str(got)[:200])


# ─────────── round 5: the gate's grammar, exactly (AGENT-SECRETS → "What the commit gate refuses") ──

def _hex(rng, n: int) -> str:
    return "".join(rng.choices("0123456789abcdef", k=n))


def _key(rng, n: int) -> str:
    """A body of a key's alphabet with a digit and a capital: never a placeholder."""
    body = rng.choices(ALPHA + "_-", k=n - 2) + [rng.choice(string.digits), rng.choice(string.ascii_uppercase)]
    rng.shuffle(body)
    return "".join(body)


def test_round5_grammar_cases() -> None:
    """Each row of the grammar table, and each round-5 counterexample in scope."""
    import random
    rng = random.Random(55)
    refused = {
        "sk_live_ + hex before the snake skip": f'K = "sk_live_{_hex(rng, 40)}"',
        "rk_live_ + hex": f'K = "rk_live_{_hex(rng, 40)}"',
        "glpat- with an underscore in the body": f'K = "glpat-abc_{_key(rng, 20)}"',
        "STRIPE_SECRET_KEY with an sk_live_ value": f'STRIPE_SECRET_KEY="sk_live_{_hex(rng, 40)}"',
        "AKIA after a literal \\n": 'K = "line\\n' + "AKIA" + _hex(rng, 16).upper().replace("A", "Q") + '"',
        "AKIA after a literal \\t": 'K = "x\\t' + "AKIA" + "Q7" * 8 + '"',
        "AKIA after a literal \\r\\n": 'K = "x\\r\\n' + "AKIA" + "Z9" * 8 + '"',
        "ASIA": 'K = "' + "ASIA" + "W4" * 8 + '"',
        "ghp_ with an underscore run": f'K = "ghp_{_key(rng, 20)}_{_key(rng, 20)}"',
        "xoxb- with an underscore run": f'K = "xoxb-{_key(rng, 20)}_{_key(rng, 20)}"',
        "lin_api_ with an underscore run": f'K = "lin_api_{_key(rng, 20)}_{_key(rng, 20)}"',
        "a real body ending in -here (no lead/end-word excuse)": f'K = "{SK}proj-{_key(rng, 30)}-here"',
        "a real body starting with your-": f'K = "{SK}proj-your-{_key(rng, 30)}"',
        "a YAML compose list item": f"  - STRIPE_SECRET_KEY={_hex(rng, 40)}",
        "a quoted YAML key": f'"STRIPE_SECRET_KEY": {_hex(rng, 40)}',
        "a JSON value with an escaped slash": '{"DEPLOY_KEY": "' + _key(rng, 20) + "\\/" + _key(rng, 20) + '"}',
    }
    for label, text in refused.items():
        got = scan_string(text, "planted.env")
        check(f"refused: {label}", bool(got["credentials"]), str(got)[:200])
    fixtures = {
        "lower-case words joined by -": f'K = "{SK}proj-your-key-goes-here-please"',
        "lower-case words joined by - after sk-or-v1-": f'K = "{OR}a-fake-key-used-only-by-this-test"',
        "one repeated character": f'K = "{SK}{"x" * 32}"',
        "an AIza placeholder": 'K = "AIza' + "z" * 30 + '"',
    }
    for label, text in fixtures.items():
        got = scan_string(text)
        check(f"a fixture, reported and not refused: {label}", not got["credentials"] and bool(got["fixtures"]),
              str(got)[:200])


def test_round5_seeded_generator() -> None:
    """Every key the grammar describes is refused: 2,400 seeded cases in six contexts."""
    import random
    sys.path.insert(0, str(ROOT))
    import credential_shape
    rng = random.Random(20261013)
    prefixes = sorted(set(credential_shape.PREFIXES) | {"sk-or-v1-", "sk-proj-", "sk-ant-api03-"})
    contexts = ('K = "{t}"', "K='{t}'", "{{\"k\": \"{t}\"}}", "x = f({t})", 'msg = "a\\n{t}"', "K={t}")
    missed = []
    for i in range(2400):
        pre = rng.choice(prefixes)
        n = max(credential_shape.BODY_MIN.get(pre, 24), 24 if pre not in credential_shape.BODY_MIN else 0)
        token = pre + _key(rng, rng.randint(n, n + 40))
        text = rng.choice(contexts).format(t=token)
        if not scan_string(text)["credentials"]:
            # The prefix and the case number only: a generated body is key-shaped,
            # and a failure message is a transcript.
            missed.append(f"case {i} {pre}…")
    check("all 2,400 generated grammar keys are refused", not missed, f"{len(missed)} missed, e.g. {missed[:3]}")


def test_round5_out_of_scope_is_documented_not_refused() -> None:
    """The gate's stated limits, pinned so any change to them is visible (AGENT-SECRETS)."""
    import random
    rng = random.Random(56)
    k = "ghp_" + _key(rng, 36)
    out = {
        "a percent-encoded prefix": 'K = "ghp%5F' + k[4:] + '"',
        "a \\u-escaped prefix": 'K = "ghp\\u005f' + k[4:] + '"',
        "an ASCII letter before the prefix": f'K = "x{k}"',
        "a Go short declaration": f'DEPLOY_KEY := "{_hex(rng, 40)}"',
        "a Python annotated assignment": f'DEPLOY_KEY: str = "{_hex(rng, 40)}"',
        "a TypeScript typed const": f'const DEPLOY_KEY: string = "{_hex(rng, 40)}"',
        "a .env value ending in ;": f"DEPLOY_KEY={_hex(rng, 40)};",
        "a lower-case name": f'deploy_key = "{_hex(rng, 40)}"',
        "a bare KEY": f"KEY={_hex(rng, 40)}",
        "a value split by implicit concatenation": f'K = "{k[:10]}" "{k[10:]}"',
        "a value split by a backslash continuation": f'K = "{k[:10]}\\\n{k[10:]}"',
        "a code-level os.environ assignment": f'os.environ["SERVICE_TOKEN"] = "{_hex(rng, 40)}"',
        "a Python dict member": f'{{"service_token": "{_hex(rng, 40)}"}}',
        "a Kubernetes name/value pair": f"- name: SERVICE_TOKEN\n  value: {_hex(rng, 40)}",
        "a YAML block scalar": f"SERVICE_TOKEN: |\n  {_hex(rng, 40)}",
        "a password that fails looks_secret": "DB_PASSWORD=correct-horse-battery-staple",
    }
    for label, text in out.items():
        got = scan_string(text, "planted.env")
        check(f"out of scope, not refused: {label}", not got["credentials"], str(got)[:200])


# ─────────── round 6: the counterexamples inside the grammar ─────────────────

def _cs():
    sys.path.insert(0, str(ROOT / "tools"))
    import check_secrets
    return check_secrets


def test_round6_counterexamples() -> None:
    import random
    rng = random.Random(66)
    aws = "AKIA" + "QZ7" * 5 + "Q"                     # exactly sixteen [0-9A-Z]
    refused_once = {
        "CRLF named form (seed 248613)": "A=1\r\nQBSFW_RKMAG_ELLCTK_KEY=890d7d839042ef08062959649f3e5c6dc913f15b\r\nB=2\r\n",
        "AKIA- then a ghp_ token": f'K = "AKIA-ghp_{_key(rng, 36)}"',
        "ASIA- then an sk_live_ token": f'K = "ASIA-sk_live_{_key(rng, 30)}"',
        "AWS id then -backup": f'K = "{aws}-backup"',
        "AWS id then _x": f'K = "{aws}_x"',
        "a bare-word value of 22 random characters": f'api_key = "{_key(rng, 22)}"',
        "a DSA private-key header": "-----BEGIN " + "DSA PRIVATE" + " KEY-----",
        "an ENCRYPTED (PKCS#8) private-key header": "-----BEGIN " + "ENCRYPTED PRIVATE" + " KEY-----",
        "ghp_ + 36 lower-case letters (one segment)": "K = \"ghp_" + "".join(rng.choices(string.ascii_lowercase, k=36)) + '"',
        "a non-ASCII letter before the prefix": f'K = "é{"ghp_" + _key(rng, 36)}"',
    }
    for label, text in refused_once.items():
        got = scan_string(text, "planted.env")
        check(f"refused: {label}", bool(got["credentials"]), str(got)[:200])
    got = scan_string(f'K = "{aws}-ghp_{_key(rng, 36)}"')
    check("AWS id then a ghp_ token: both refused", len(got["credentials"]) == 2, str(got)[:300])
    got = scan_string('K = "sk_your_key_goes_here_please"')
    check("an _-joined placeholder is a fixture, not silence", not got["credentials"] and bool(got["fixtures"]),
          str(got)[:200])
    cs = _cs()
    saved = dict(cs.credential_shape.BODY_MIN)
    cs.credential_shape.BODY_MIN["ghp_"] = 36
    try:
        short, full = scan_string(f'K = "ghp_{_key(rng, 30)}"'), scan_string(f'K = "ghp_{_key(rng, 36)}"')
    finally:
        cs.credential_shape.BODY_MIN.clear(); cs.credential_shape.BODY_MIN.update(saved)
    check("a vendor minimum above 24 is honoured", not short["credentials"] and bool(full["credentials"]),
          f"{short} / {full}"[:300])
    check("SKIP no longer names docs/DECISIONS.md, which the engine folder does not hold",
          "docs/DECISIONS.md" not in cs.SKIP)


def test_round6_files_are_read_as_bytes_and_an_unreadable_one_fails() -> None:
    """A non-UTF-8 file is scanned (surrogateescape); a file that will not read is a failure."""
    import random
    rng = random.Random(67)
    cs = _cs()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sec-bytes-")).resolve()
    (d / "latin.txt").write_bytes(b"\xff\xfe caf\xe9 K = \"" + ("ghp_" + _key(rng, 36)).encode() + b"\"\n")
    (d / "locked.txt").write_text("x")
    (d / "locked.txt").chmod(0)
    try:
        creds, fixtures, allowed, unreadable = cs.scan_tree(d, ["latin.txt", "locked.txt"])
    finally:
        (d / "locked.txt").chmod(0o600)
    check("a key in a non-UTF-8 file is refused", any(h["where"].startswith("latin.txt") for h in creds), str(creds)[:200])
    check("an unreadable file is reported as a failure, never skipped",
          os.geteuid() == 0 or [u["path"] for u in unreadable] == ["locked.txt"], str(unreadable))


def test_round6_generator_crlf_and_non_utf8() -> None:
    """600 named secrets on CRLF lines and 600 prefixed tokens inside non-UTF-8 bytes: all refused."""
    import random
    sys.path.insert(0, str(ROOT))
    import credential_shape
    rng = random.Random(248613)
    cs = _cs()
    sys.path.insert(0, str(ROOT / "collectors"))
    import scan_env
    missed = []
    for i in range(600):
        name = "_".join("".join(rng.choices(string.ascii_uppercase, k=rng.randint(3, 6))) for _ in range(rng.randint(1, 3)))
        name += rng.choice(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD"))
        # Constrained to the grammar: the named rule judges a value that passes
        # `looks_secret` (a 40-hex draw under its 3.4 entropy floor does not).
        value = _hex(rng, rng.choice((40, 64)))
        while not scan_env.looks_secret(value):
            value = _hex(rng, len(value))
        text = f"A=1\r\n{name}={value}\r\nB=2\r\n"
        if not scan_string(text, "planted.env")["credentials"]:
            missed.append(f"crlf case {i}")
    prefixes = sorted(set(credential_shape.PREFIXES) | {"sk-or-v1-", "sk-proj-"})
    for i in range(600):
        pre = rng.choice(prefixes)
        token = pre + _key(rng, rng.randint(24, 60))
        raw = bytes(rng.choices(range(0x80, 0x100), k=5)) + b' K = "' + token.encode() + b'"\n'
        if not [h for h in cs.scan_text(cs.decode(raw), "latin.txt") if h.get("credential")]:
            missed.append(f"bytes case {i} {pre}…")
    check("all 1,200 CRLF and non-UTF-8 cases are refused", not missed, f"{len(missed)} missed, e.g. {missed[:3]}")


def _mixed(rng, n: int) -> str:
    """n characters with a digit, a capital and a lower-case letter, otherwise random."""
    body = rng.choices(ALPHA, k=n - 3) + [rng.choice(string.digits), rng.choice(string.ascii_uppercase),
                                          rng.choice(string.ascii_lowercase)]
    rng.shuffle(body)
    return "".join(body)


def _secret_hex(rng, n: int) -> str:
    sys.path.insert(0, str(ROOT / "collectors"))
    import scan_env
    while True:
        v = _hex(rng, n)
        if scan_env.looks_secret(v):
            return v


def test_round7_most_severe_verdict_wins_and_the_whole_value_is_measured() -> None:
    """Round 7: a non-refusing verdict at a position never hides a refusing one;
    the bare-word rule measures the WHOLE value (no prefix stripped)."""
    import random
    rng = random.Random(77)
    pad = "sk-" + "a" * 24
    refused = {
        "API_KEY=\"<40 hex>\" (bare word fixture vs named refusal)": f'API_KEY="{_secret_hex(rng, 40)}"',
        "API_KEY: '<64 hex>'": f"API_KEY: '{_secret_hex(rng, 64)}'",
        "export API_KEY=\"<64 hex>\"": f'export API_KEY="{_secret_hex(rng, 64)}"',
        "a placeholder-led value under a named secret": f'X_SECRET_KEY="{pad}/{_mixed(rng, 33)}"',
        "a placeholder-led value under a bare word": f'api_key = "{pad}+{_mixed(rng, 31)}"',
        "Password\\t=\\t\"hf_<17>\"": f'Password\t=\t"hf_{_mixed(rng, 17)}"',
        "SECRET:'AKIA<17>'": "SECRET:'AKIA" + _mixed(rng, 17) + "'",
        "token = \"hf_<17>\"": f'token = "hf_{_mixed(rng, 17)}"',
        "a BOM before the first .env line": "\ufeff" + f"DEPLOY_KEY={_secret_hex(rng, 40)}\n",
        "CR-only line endings": f"A=1\rDEPLOY_KEY={_secret_hex(rng, 40)}\rB=2\r",
        "a JSON member keyed by a bare word": '{"api_key": "' + _mixed(rng, 22) + '"}',
    }
    for label, text in refused.items():
        got = scan_string(text, "planted.env")
        check(f"refused: {label}", bool(got["credentials"]), str(got)[:200])
    # A SLOT ADDRESS names a secret; it is not one (`project/env/NAME`, the vault's own form).
    for addr in ('{"secret": "alpha/prod/STRIPE_KEY"}', 'secret = "beta-api/prod/DATABASE_URL"',
                 '{"secret": "a/OPENROUTER_API_KEY_PREVIOUS"}'):
        got = scan_string(addr)
        check(f"a slot address under a bare word is not refused: {addr[:24]}…", not got["credentials"],
              str(got)[:200])
    utf16 = ('K = "ghp_' + _key(rng, 36) + '"').encode("utf-16")
    got = scan_string(_cs().decode(utf16))
    check("out of scope, not refused: a UTF-16 file", not got["credentials"], str(got)[:200])


def test_round7_generator_overlaps_bom_and_cr() -> None:
    """1,200 named-plus-bare-word overlaps, with BOM and CR-only contexts: all refused."""
    import random
    rng = random.Random(7748)
    cs = _cs()
    named = ("API_KEY", "X_SECRET_KEY", "DEPLOY_TOKEN", "DB_PASSWORD")
    bare = ("api_key", "api-key", "apikey", "secret", "SECRET", "token", "Password", "password")
    pad = "sk-" + "a" * 24
    missed = []

    def bare_value() -> str:
        while True:
            kind = rng.choice(("plain", "prefixed", "padded"))
            v = (_mixed(rng, rng.randint(20, 40)) if kind == "plain" else
                 rng.choice(("hf_", "AKIA", "ghp_")) + _mixed(rng, rng.randint(17, 30)) if kind == "prefixed" else
                 pad + "+" + _mixed(rng, rng.randint(24, 40)))
            rest = v[len(pad) + 1:] if kind == "padded" else v
            if cs.normalised(rest) >= cs.NORMALISED_FLOOR:   # constrained to the grammar's floor
                return v

    for i in range(1200):
        if i % 2:
            name, value = rng.choice(named), rng.choice((_secret_hex(rng, 40), pad + "/" + _mixed(rng, 30),
                                                          "ghp_" + _key(rng, 36)))
            line = rng.choice(('{n}="{v}"', "{n}: '{v}'", 'export {n}="{v}"', "{n}={v}")).format(n=name, v=value)
        else:
            name, value = rng.choice(bare), bare_value()
            line = rng.choice(('{n} = "{v}"', '{n}\t=\t"{v}"', "{n}:'{v}'", '{{"{n}": "{v}"}}')).format(n=name, v=value)
        ctx = rng.choice(("plain", "bom", "cr", "crlf"))
        text = {"plain": "A=1\n{l}\nB=2\n", "bom": "\ufeff{l}\nB=2\n", "cr": "A=1\r{l}\rB=2\r",
                "crlf": "A=1\r\n{l}\r\nB=2\r\n"}[ctx].format(l=line)
        if not scan_string(text, "planted.env")["credentials"]:
            missed.append(f"case {i} {name} {ctx}")
    check("all 1,200 overlap/BOM/CR cases are refused", not missed, f"{len(missed)} missed, e.g. {missed[:4]}")


def test_round8_leading_underscores_and_the_cheap_lows() -> None:
    """Round 8: F1 (leading underscores), L3 (hex under a bare word), L4 (JSON `\\/`),
    L5 (slot addresses are word segments), L8 (line separators, a mid-file BOM, a
    trailing space inside the quotes)."""
    import random
    rng = random.Random(88)
    b62 = _mixed(rng, 32)
    refused = {
        "_API_KEY = \"<32 base62>\"": f'_API_KEY = "{b62}"',
        "__DB_PASSWORD = \"<40 hex>\"": f'__DB_PASSWORD = "{_secret_hex(rng, 40)}"',
        "_DEPLOY_TOKEN=<40 hex>": f"_DEPLOY_TOKEN={_secret_hex(rng, 40)}\n",
        "export _DEPLOY_TOKEN=<40 hex>": f"export _DEPLOY_TOKEN={_secret_hex(rng, 40)}\n",
        "{\"_API_KEY\": \"…\"}": '{"_API_KEY": "' + _mixed(rng, 32) + '"}',
        "  _API_KEY: …": f"  _API_KEY: {_secret_hex(rng, 40)}\n",
        "self._API_KEY = \"…\"": f'self._API_KEY = "{_mixed(rng, 32)}"',
        "token = \"<40 hex>\" (L3)": f'token = "{_secret_hex(rng, 40)}"',
        "a JSON \\/ under a bare word (L4)": '{"api_key": "' + _mixed(rng, 12) + "\\/" + _mixed(rng, 13) + '"}',
        # The verifier's three inputs, composed: the gate now judges them, so a
        # literal of one in this file would be refused by the self-scan.
        "L5: digits in the first segment": 'token = "' + "8gru7kmvbve40" + "treaetij76n/EOX8F36R6UGX" + '"',
        "L5: an hf_ value with a one-letter name": 'token = "' + "hf_rtd399" + "xgj31tj2hmw/X" + '"',
        "L5: digit-mixed segments": 'api_key = "' + "mtdeecy4bypo2ma2/" + "v4ohowysg07ywblj/V1" + '"',
        "L8: U+2028 before a .env line": f"A=1 DEPLOY_KEY={_secret_hex(rng, 40)} B=2",
        "L8: NEL before a .env line": f"A=1\u0085DEPLOY_KEY={_secret_hex(rng, 40)}\n",
        "L8: FF before a .env line": f"A=1\x0cDEPLOY_KEY={_secret_hex(rng, 40)}\n",
        "L8: VT before a .env line": f"A=1\x0bDEPLOY_KEY={_secret_hex(rng, 40)}\n",
        "L8: a BOM at a mid-file line start": f"A=1\n﻿DEPLOY_KEY={_secret_hex(rng, 40)}\n",
        "L8: a trailing space inside the quotes": f'DEPLOY_KEY = "{_secret_hex(rng, 40)} "',
    }
    for label, text in refused.items():
        got = scan_string(text, "planted.env")
        check(f"refused: {label}", bool(got["credentials"]), str(got)[:200])
    for label, text in {"a bare _KEY stays out of scope": f"_KEY={_secret_hex(rng, 40)}\n",
                        "a lower-case name with a leading underscore": f'_deploy_key = "{_secret_hex(rng, 40)}"',
                        "a slot address of word segments": 'token = "alpha-web/prod/STRIPE_KEY"'}.items():
        got = scan_string(text, "planted.env")
        check(f"not refused: {label}", not got["credentials"], str(got)[:200])


def test_round8_history_is_read_as_bytes() -> None:
    """L2: `--history` kept a lone CR's line and survives a non-UTF-8 byte."""
    import random
    rng = random.Random(89)
    cs = _cs()
    raw = (b"diff --git a/x.env b/x.env\n+++ b/x.env\n@@ -0,0 +1,2 @@\n+A=1\rDEPLOY_KEY="
           + _secret_hex(rng, 40).encode() + b"\n"
           b"+caf\xe9 \xff\xfe\ndiff --git a/y.txt b/y.txt\n+++ b/y.txt\n@@ -0,0 +1 @@\n+ok\n")
    per_file = cs.history_files(raw)
    check("each added line is kept, the lone CR inside it too", "\r" in "".join(per_file.get("x.env", [])),
          str({k: len(v) for k, v in per_file.items()}))
    hits = [h for h in cs.scan_text("\n".join(per_file["x.env"]), "history:x.env") if h.get("credential")]
    check("and the named value after the CR is refused", bool(hits), str(hits)[:200])
    check("a non-UTF-8 byte does not raise", "y.txt" in per_file, str(list(per_file)))


def test_round9_nested_names_slots_and_the_cheap_lows() -> None:
    """Round 9: M-G1a (inner double underscores), the slot name's two characters,
    L3 (slot variants the vault allows), L4 (handled cases, and the dotted value
    named out of scope)."""
    import random
    rng = random.Random(99)
    hx = lambda: _secret_hex(rng, 40)  # noqa: E731
    refused = {
        "APP__DB_PASSWORD=<40 hex>": f"APP__DB_PASSWORD={hx()}\n",
        "APP__DB_PASSWORD = \"…\"": f'APP__DB_PASSWORD = "{hx()}"',
        "{\"APP__DB_PASSWORD\": \"…\"}": '{"APP__DB_PASSWORD": "' + hx() + '"}',
        "  APP__DB_PASSWORD: …": f"  APP__DB_PASSWORD: {hx()}\n",
        "MYAPP__STRIPE__API_KEY=…": f"MYAPP__STRIPE__API_KEY={hx()}\n",
        "L4: a leading space inside the quotes": f'DEPLOY_KEY = " {hx()}"',
        "L4: a leading tab inside the quotes, under a bare word": f'token = "\t{hx()}"',
        "L4: NBSP YAML indentation": f"\u00a0\u00a0DEPLOY_KEY: {hx()}\n",
        "L4: a first word that starts with a digit": f"_2FA_SECRET={hx()}\n",
    }
    for label, text in refused.items():
        got = scan_string(text, "planted.env")
        check(f"refused: {label}", bool(got["credentials"]), str(got)[:200])
    slot = _cs().SLOT_ADDRESS
    check("a slot name has two or more characters", slot.fullmatch("alpha/prod/X") is None
          and slot.fullmatch("alpha/prod/XY") is not None)
    for label, text in {"a bare __KEY": f"__KEY={hx()}\n",
                        "a bare _KEY": f"_KEY={hx()}\n",
                        "a slot address with an upper-case project": '{"secret": "Alpha-Web/prod/STRIPE_KEY"}',
                        "a slot address with a _-led name": 'token = "alpha/prod/_STRIPE_KEY"',
                        "a placeholder is a fixture before looks_secret": 'API_KEY = "sk_your_key_goes_here"',
                        "a placeholder under a bare word, too": 'api_key = "sk_your_key_goes_here"',
                        "out of scope: a JWT under a bare word": 'token = "' + "eyJ" + _mixed(rng, 20) + "." + _mixed(rng, 30) + "." + _mixed(rng, 20) + '"'}.items():
        got = scan_string(text, "planted.env")
        check(f"not refused: {label}", not got["credentials"], str(got)[:200])


def test_round9_history_headers_are_a_state_machine() -> None:
    """M-G1b: a C-quoted path, a spoofed `+++ b/` added line and a `++i;` line."""
    cs = _cs()
    raw = (b"commit 1\n"
           b"diff --git a/plain.txt b/plain.txt\n--- a/plain.txt\n+++ b/plain.txt\n@@ -0,0 +1,3 @@\n"
           b"+first\n+++ b/spoofed.txt\n+++i;\n"
           b"diff --git \"a/sp ace\\ttab.txt\" \"b/sp ace\\ttab.txt\"\n--- /dev/null\n"
           b"+++ \"b/sp ace\\ttab.txt\"\n@@ -0,0 +1 @@\n+quoted\n"
           b"diff --git a/caf\xc3\xa9.txt b/caf\xc3\xa9.txt\n+++ b/caf\xc3\xa9.txt\n@@ -0,0 +1 @@\n+accent\n")
    got = cs.history_files(raw)
    check("an added `++ b/…` line is a line, not a header", got.get("plain.txt") == ["first", "++ b/spoofed.txt", "++i;"],
          str(got.get("plain.txt")))
    check("so no file is invented from it", "spoofed.txt" not in got, str(list(got)))
    check("a C-quoted path is unquoted", got.get("sp ace\ttab.txt") == ["quoted"], str(list(got)))
    check("an unquoted UTF-8 path is kept", got.get("café.txt") == ["accent"], str(list(got)))


def test_round9_tracked_names_survive_any_bytes() -> None:
    """L1: `git ls-files -z`, so a non-ASCII name is not quoted into a name that does not exist."""
    cs = _cs()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sec-lsz-")).resolve()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    (d / "café.txt").write_text("x")
    for args in (("init", "-q"), ("add", "-A")):
        subprocess.run(["git", *args], cwd=d, env=env, check=True, capture_output=True, timeout=60)
    names = cs.tracked(d)
    check("a non-ASCII tracked name comes back as it is", names == ["café.txt"], str(names))
    _c, _f, _a, unreadable = cs.scan_tree(d, names)
    check("and is read, not UNREADABLE", not unreadable, str(unreadable))


def _repo(prefix: str):
    """A throw-away repository on `main`, git's user configuration shut out, and a `git` caller."""
    d = pathlib.Path(tmpdir.mkdtemp(prefix=prefix)).resolve()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

    def git(*args, check=True):
        return subprocess.run(["git", *args], cwd=d, env=env, check=check, capture_output=True, timeout=60)
    git("init", "-q", "-b", "main")
    return d, git


def _history_refusals(d, root=None) -> tuple[dict, dict]:
    """(added lines per file, refusals per file) of the history mode over repository `d`,
    or over its folder `root` as the gate reads the engine folder."""
    cs = _cs()
    per_file = cs.history_files(cs.history_log(root or d, 20))
    found = {}
    for rel, lines in per_file.items():
        n = sum(1 for h in cs.scan_text("\n".join(lines), f"history:{rel}") if h.get("credential"))
        if n:
            found[rel] = n
    return per_file, found


def test_round10_history_reads_binary_marked_files_as_text() -> None:
    """M-1: `git log -p` printed "Binary files differ" for a NUL-holding file and for
    one marked `-diff`, so their added lines were never scanned. `--text` reads them."""
    import random
    rng = random.Random(1010)
    tok = "ghp" + "_" + _mixed(rng, 36)
    d, git = _repo("observatory-sec-bin-")
    (d / ".gitattributes").write_text("*.lock -diff\n")
    (d / "deps.lock").write_text(f"pinned {tok}\n")
    (d / "blob.dat").write_bytes(b"\x00\x01head\nv " + tok.encode() + b"\n")
    git("add", "-A"); git("commit", "-q", "-m", "add")
    (d / "deps.lock").write_text("pinned\n")
    (d / "blob.dat").write_bytes(b"\x00\x01head\n")
    git("add", "-A"); git("commit", "-q", "-m", "remove")
    _per, found = _history_refusals(d)
    check("a `-diff` file's added token is refused from history", bool(found.get("deps.lock")), str(found))
    check("a NUL-holding file's added token is refused from history", bool(found.get("blob.dat")), str(found))


def test_round10_history_diffs_merge_resolutions() -> None:
    """M-2: a merge's own resolution was never diffed; `--diff-merges=first-parent` diffs it."""
    import random
    rng = random.Random(1011)
    tok = "ghp" + "_" + _mixed(rng, 36)
    d, git = _repo("observatory-sec-merge-")
    (d / "m.txt").write_text("base\n"); git("add", "-A"); git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "side")
    (d / "m.txt").write_text("side\n"); git("commit", "-q", "-am", "side")
    git("checkout", "-q", "main")
    (d / "m.txt").write_text("main\n"); git("commit", "-q", "-am", "main")
    git("merge", "-q", "side", check=False)
    (d / "m.txt").write_text(f"resolved {tok}\n"); git("add", "m.txt"); git("commit", "-q", "--no-edit")
    (d / "m.txt").write_text("resolved\n"); git("commit", "-q", "-am", "remove")
    _per, found = _history_refusals(d)
    check("a token written by a merge resolution is refused from history", bool(found.get("m.txt")), str(found))


def test_round10_history_names_paths_whatever_the_prefix_config() -> None:
    """L-2: a repository's `diff.noprefix=true` drops `a/`/`b/`, so a real `b/…` path
    lost its first folder. The history mode passes its own prefixes."""
    import random
    rng = random.Random(1012)
    d, git = _repo("observatory-sec-noprefix-")
    git("config", "diff.noprefix", "true")
    (d / "b").mkdir()
    (d / "b" / "inner.env").write_text("DEPLOY_KEY=" + _secret_hex(rng, 40) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "add")
    per, found = _history_refusals(d)
    check("the path is the real one, `b/` folder kept", "b/inner.env" in per, str(list(per)))
    check("and its value is refused there", bool(found.get("b/inner.env")), str(found))


def test_round10_slot_names_as_wide_as_the_vault() -> None:
    """L-1: the vault accepts `[A-Z_][A-Z0-9_]{0,127}`; a slot name now takes `_+` joins,
    a digit-first word after `_` and a trailing `_`, with two characters after its underscores."""
    slot = _cs().SLOT_ADDRESS
    for addr in ("alpha-web/prod/APP__DB_PASSWORD", "alpha-web/prod/_2FA_SECRET", "alpha-web/prod/KEY_",
                 "alpha-web/prod/STRIPE_KEY", "alpha-web/prod/XY"):
        check(f"a slot address: {addr}", slot.fullmatch(addr) is not None)
    for addr in ("alpha-web/prod/X", "alpha-web/prod/_X", "alpha-web/prod/__", "alpha-web/prod/2FA"):
        check(f"not a slot address: {addr}", slot.fullmatch(addr) is None)
    got = scan_string('token = "alpha-web/prod/APP__DB_PASSWORD"', "planted.env")
    check("a nested slot address under a bare word is not refused", not got["credentials"], str(got)[:200])


def test_round11_history_shows_the_root_commit() -> None:
    """L3: `log.showRoot=false` hid the root commit's diff; `--root` shows it."""
    import random
    d, git = _repo("observatory-sec-root-")
    git("config", "log.showRoot", "false")
    (d / "k.env").write_text("DEPLOY_KEY=" + _secret_hex(random.Random(1101), 40) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "root")
    _per, found = _history_refusals(d)
    check("a key added by the root commit is refused from history", bool(found.get("k.env")), str(found))


def test_round11_history_ignores_replace_refs() -> None:
    """L4: a `refs/replace/` stand-in made git show another commit in place of the
    one that added a key; the history mode reads the real objects."""
    import random
    d, git = _repo("observatory-sec-replace-")
    (d / "base.txt").write_text("base\n"); git("add", "-A"); git("commit", "-q", "-m", "base")
    base_tree = git("rev-parse", "HEAD^{tree}").stdout.decode().strip()
    base = git("rev-parse", "HEAD").stdout.decode().strip()
    (d / "k.env").write_text("DEPLOY_KEY=" + _secret_hex(random.Random(1102), 40) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "add key")
    real = git("rev-parse", "HEAD").stdout.decode().strip()
    stand_in = git("commit-tree", base_tree, "-p", base, "-m", "add key").stdout.decode().strip()
    git("replace", real, stand_in)
    (d / "k.env").unlink(); (d / "other.txt").write_text("x\n")
    git("add", "-A"); git("commit", "-q", "-m", "remove key")
    _per, found = _history_refusals(d)
    check("a key hidden behind a replace ref is refused from history", bool(found.get("k.env")), str(found))


def test_round11_history_walks_side_branches() -> None:
    """L6: with a pathspec git simplifies history, and a side branch that added a
    key and removed it before the merge vanished; `--full-history` keeps it. The files
    live in a subfolder read as the engine folder is (`-- .` there is a real pathspec)."""
    import random
    d, git = _repo("observatory-sec-side-")
    sub = d / "engine"; sub.mkdir()
    (sub / "m.txt").write_text("base\n"); git("add", "-A"); git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "side")
    (sub / "s.env").write_text("DEPLOY_KEY=" + _secret_hex(random.Random(1103), 40) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "side adds")
    (sub / "s.env").unlink(); git("add", "-A"); git("commit", "-q", "-m", "side removes")
    git("checkout", "-q", "main")
    (sub / "m.txt").write_text("main\n"); git("commit", "-q", "-am", "main")
    git("merge", "-q", "--no-edit", "side")
    _per, found = _history_refusals(d, sub)
    check("a key added and removed on a side branch is refused from history", bool(found.get("s.env")), str(found))


def test_round11_history_quoted_name_with_a_space() -> None:
    """L2: git appends a TAB after a header name holding a space, quoted or not; the
    TAB is stripped before the quote test, so a quoted name is unquoted."""
    import random
    cs = _cs()
    check("a quoted name with a space, a quote and git's TAB",
          cs._header_path('"b/a \\"q\\" c.txt"\t') == 'a "q" c.txt', str(cs._header_path('"b/a \\"q\\" c.txt"\t')))
    d, git = _repo("observatory-sec-quote-")
    (d / 'a "q" c.env').write_text("DEPLOY_KEY=" + _secret_hex(random.Random(1104), 40) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "add")
    per, found = _history_refusals(d)
    check("the real file's name comes back", 'a "q" c.env' in per, str(list(per)))


def test_round11_slot_name_at_most_128() -> None:
    """L1: the vault's names are at most 128 characters, leading underscores included."""
    slot = _cs().SLOT_ADDRESS
    for n, want in ((128, True), (129, False)):
        name = "_" + "A" * (n - 1)
        check(f"a {n}-character `_`-led slot name is {'' if want else 'not '}a slot name",
              (slot.fullmatch("alpha/prod/" + name) is not None) is want)
    check("so is a 128-character name without underscores", slot.fullmatch("alpha/prod/" + "B" * 128) is not None)
    check("and not a 129-character one", slot.fullmatch("alpha/prod/" + "B" * 129) is None)


def test_round11_tree_scan_never_follows_links() -> None:
    """L5: a tracked symbolic link is judged by its target TEXT and never followed; any
    other non-regular file is unreadable. Run in a child with a timeout: following a
    link to a FIFO blocks for ever."""
    import random
    rng = random.Random(1105)
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sec-links-")).resolve()
    outside = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sec-outside-")).resolve()
    os.mkfifo(outside / "pipe")
    (outside / "secret.env").write_text("DEPLOY_KEY=" + _secret_hex(rng, 40) + "\n")
    os.symlink(outside / "pipe", d / "to-fifo")
    os.symlink(outside / "secret.env", d / "to-outside")
    os.symlink("ghp" + "_" + _mixed(rng, 36), d / "token-text")
    os.mkfifo(d / "fifo")
    code = ("import json, pathlib, sys; sys.path.insert(0, sys.argv[1]); import check_secrets as cs; "
            "c, f, a, u = cs.scan_tree(pathlib.Path(sys.argv[2]), ['to-fifo', 'to-outside', 'token-text', 'fifo']); "
            "print(json.dumps({'refused': sorted({h['where'].split(':')[0] for h in c}), "
            "'unreadable': sorted(x['path'] for x in u)}))")
    try:
        p = subprocess.run([sys.executable, "-c", code, str(ROOT / "tools"), str(d)], capture_output=True,
                           text=True, timeout=60)
    except subprocess.TimeoutExpired:
        check("the tree scan returns (a link to a FIFO is not followed)", False, "timed out after 60 s")
        return
    check("the tree scan returns (a link to a FIFO is not followed)", p.returncode == 0, p.stderr[-300:])
    try:
        got = json.loads(p.stdout)
    except ValueError:
        check("the child answers in JSON", False, p.stdout[-200:])
        return
    check("a link to an outside file is not judged under the link's name", "to-outside" not in got["refused"],
          str(got))
    check("a link whose target text is a token is refused", "token-text" in got["refused"], str(got))
    check("a FIFO at a tracked path is unreadable", got["unreadable"] == ["fifo"], str(got))


def test_round11_issuers_are_known_prefixes() -> None:
    """L7: the bare-word rule has no "rest after a leading token" arm, because
    `looks_secret` refuses any value led by an issuer prefix — true only while every
    issuer prefix is one of `scan_env.KNOWN_PREFIX`. This pins that."""
    import random
    cs = _cs()
    known = tuple(cs.scan_env.KNOWN_PREFIX)
    missing = [p for p in cs.ISSUERS if not p.startswith(known)]
    check("every issuer prefix is a known prefix of the env classifier", not missing, str(missing))
    rng = random.Random(1106)
    for pre in ("sk_your_key_goes_here", "ghp" + "_" + "short"):
        text = 'token = "' + pre + "+" + _mixed(rng, 24) + '"'
        check(f"a value led by {pre[:3]}… with a random rest is refused",
              bool(scan_string(text, "planted.env")["credentials"]))


def test_round13_hex_values_are_refused_whatever_their_entropy() -> None:
    """MEDIUM (round 13): a 32- or 40-hex value under a named secret or a bare word
    passed when its entropy fell under `looks_secret`'s 3.4 (about 1% of 40-hex and
    6.5% of 32-hex values). A run of 32+ hex characters is a credential, as in
    `credential_shape`. Seeded: 1,000 values under each form."""
    import random
    cs = _cs()
    rng = random.Random(1301)
    names = ("DEPLOY_KEY", "MD_PASSWORD", "GH_TOKEN", "APP_SECRET")
    forms = {
        "NAME = \"v\"": lambda n, v: f'{n} = "{v}"',
        "NAME=v (.env)": lambda n, v: f"{n}={v}\n",
        "export NAME=v": lambda n, v: f"export {n}={v}\n",
        "  NAME: v (YAML)": lambda n, v: f"  {n}: {v}\n",
        "{\"NAME\": \"v\"} (JSON)": lambda n, v: '{"' + n + '": "' + v + '"}',
    }
    for word in ("api_key", "api-key", "apikey", "secret", "token", "passwd", "password"):
        forms[f"{word} = 'v'"] = (lambda w: lambda _n, v: f"{w} = '{v}'")(word)
    for label, make in forms.items():
        missed = []
        for i in range(1000):
            v = "".join(rng.choice("0123456789abcdef") for _ in range(32 if i % 2 else 40))
            if i % 5 == 0:
                v = v.upper()
            if not [h for h in cs.scan_text(make(names[i % len(names)], v), "planted.env") if h.get("credential")]:
                missed.append(i)
        check(f"every 32/40-hex value is refused: {label}", not missed, f"{len(missed)} missed, first case {missed[:3]}")
    for text in ("DEPLOY_KEY=" + "7c668685694c2bcf" + "7b753282cb9263b636b5577e",
                 "export MD_PASSWORD=" + "c177adca548484f9" + "dac5afa1aa558a9f",
                 "apikey = '" + "f3aabe341b2ea9de" + "d33826983aa633d9" + "'"):
        check(f"the verifier's counterexample is refused: {text.split('=')[0].strip()}",
              bool(scan_string(text, "planted.env")["credentials"]))


def test_round13_history_ignores_grafts() -> None:
    """L2: `.git/info/grafts` re-parented a commit past the one that added a key;
    `GIT_GRAFT_FILE` points at an empty file for the history mode."""
    import random
    d, git = _repo("observatory-sec-grafts-")
    (d / "base.txt").write_text("base\n"); git("add", "-A"); git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD").stdout.decode().strip()
    (d / "k.env").write_text("DEPLOY_KEY=" + _secret_hex(random.Random(1302), 40) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "add key")
    (d / "k.env").unlink(); git("add", "-A"); git("commit", "-q", "-m", "remove key")
    head = git("rev-parse", "HEAD").stdout.decode().strip()
    (d / ".git" / "info").mkdir(exist_ok=True)
    (d / ".git" / "info" / "grafts").write_text(f"{head} {base}\n")
    _per, found = _history_refusals(d)
    check("a key behind a graft is refused from history", bool(found.get("k.env")), str(found))


def test_round13_history_reads_renames_and_copies_as_content() -> None:
    """L3: with rename detection a file renamed or copied out of a SKIP path showed no
    lines, so its key went unread; `--no-renames` shows the whole new file."""
    import random
    rng = random.Random(1303)
    for how in ("rename", "copy"):
        d, git = _repo(f"observatory-sec-{how}-")
        git("config", "diff.renames", "copies")
        (d / "tests").mkdir()
        (d / "tests" / "test_secrets.py").write_text("DEPLOY_KEY=" + _secret_hex(rng, 40) + "\n")
        git("add", "-A"); git("commit", "-q", "-m", "planted where SKIP applies")
        if how == "rename":
            git("mv", "tests/test_secrets.py", "leaked.env")
        else:
            # Git pairs a copy only with a source changed in the same commit (round 14:
            # without this the copy case could never catch anything).
            (d / "leaked.env").write_bytes((d / "tests" / "test_secrets.py").read_bytes())
            with open(d / "tests" / "test_secrets.py", "a") as fh:
                fh.write("# the source changes in the copy's commit\n")
            git("add", "-A")
        git("commit", "-q", "-m", how)
        _per, found = _history_refusals(d)
        done = {"rename": "renamed", "copy": "copied"}[how]
        check(f"a key {done} out of a SKIP path is refused from history", bool(found.get("leaked.env")), str(found))


def test_round13_tree_scan_never_follows_a_parent_link() -> None:
    """L4: only the last component was opened with O_NOFOLLOW, so a tracked path under
    a symbolic-link FOLDER was read through the link. Every component is now opened
    with O_NOFOLLOW|O_DIRECTORY; a path under a link is unreadable (the gate fails)."""
    import random
    rng = random.Random(1304)
    cs = _cs()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sec-parent-")).resolve()
    outside = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sec-parent-out-")).resolve()
    (outside / "k.env").write_text("DEPLOY_KEY=" + _secret_hex(rng, 40) + "\n")
    os.symlink(outside, d / "sub")
    c, _f, _a, u = cs.scan_tree(d, ["sub/k.env"])
    check("an outside key behind a parent link is not read", not c, str([h.get("where") for h in c]))
    check("the path under the link is unreadable, named so", [x["path"] for x in u] == ["sub/k.env"]
          and "symbolic link" in u[0]["why"], str(u))
    # Test 2: a COMMITTED key whose folder is swapped, in the working tree, for a link to a benign copy.
    d2, git = _repo("observatory-sec-parent-commit-")
    (d2 / "sub").mkdir()
    (d2 / "sub" / "k.env").write_text("DEPLOY_KEY=" + _secret_hex(rng, 40) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "a committed key")
    (d2 / "benign").mkdir(); (d2 / "benign" / "k.env").write_text("DEPLOY_KEY=\n")
    (d2 / "sub" / "k.env").unlink(); (d2 / "sub").rmdir(); os.symlink("benign", d2 / "sub")
    got = cs.scan_repo(d2)
    check("the benign copy behind the link is not read in the key's place, and the path fails the gate",
          [x["path"] for x in got["unreadable"]] == ["sub/k.env"], str(got["unreadable"]))


def test_round13_gitlinks_skipped_and_index_entries_read() -> None:
    """L5: a gitlink (a submodule) is skipped and named; a skip-worktree entry (sparse
    checkout) and a tracked file missing from the working tree are read from the index."""
    import random
    rng = random.Random(1305)
    cs = _cs()
    d, git = _repo("observatory-sec-sparse-")
    (d / "keep").mkdir(); (d / "keep" / "a.txt").write_text("a\n")
    (d / "keep" / "gone.env").write_text("DEPLOY_KEY=" + _secret_hex(rng, 40) + "\n")
    (d / "hidden").mkdir(); (d / "hidden" / "k.env").write_text("DEPLOY_KEY=" + _secret_hex(rng, 40) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "files")
    head = git("rev-parse", "HEAD").stdout.decode().strip()
    git("update-index", "--add", "--cacheinfo", f"160000,{head},sub")
    git("commit", "-q", "-m", "a submodule")
    git("sparse-checkout", "set", "--no-cone", "/keep/")
    (d / "keep" / "gone.env").unlink()
    got = cs.scan_repo(d)
    refused = sorted({h["where"].split(":")[0] for h in got["credentials"]})
    check("a skip-worktree entry is read from the index", "hidden/k.env" in refused, str(refused))
    check("a tracked file missing from the working tree is read from the index", "keep/gone.env" in refused,
          str(refused))
    check("nothing is unreadable", not got["unreadable"], str(got["unreadable"]))
    check("the gitlink is skipped and named", [x["path"] for x in got["skipped"]] == ["sub"], str(got["skipped"]))


def test_round13_large_file_is_unreadable() -> None:
    """L6: one file's read is capped (32 MB); a larger one is unreadable, with the reason."""
    cs = _cs()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sec-big-")).resolve()
    with open(d / "big.bin", "wb") as fh:
        fh.truncate(cs.MAX_FILE_BYTES + 1)  # sparse: no disk is used
    _c, _f, _a, u = cs.scan_tree(d, ["big.bin"])
    check("a file over the cap is unreadable, saying why", [x["path"] for x in u] == ["big.bin"]
          and "larger than" in u[0]["why"], str(u))


def test_round13_text_output_gives_the_real_reason() -> None:
    """L7: the text output printed `normalised X < 0.78` for every fixture, placeholders too."""
    cs = _cs()
    hit = scan_string('API_KEY = "sk_your_key_goes_here"', "planted.env")["fixtures"][0]
    line = cs.text_line("fixture", hit)
    check("a placeholder fixture's line says it is a placeholder", "placeholder" in line and "normalised" not in line,
          line)


def test_round14_index_blob_ignores_replace_refs() -> None:
    """L1: a tracked file missing from the working tree is read from its index blob;
    a `refs/replace/` stand-in for that blob must not be read in its place."""
    import random
    cs = _cs()
    d, git = _repo("observatory-sec-blob-replace-")
    (d / "k.env").write_text("DEPLOY_KEY=" + _secret_hex(random.Random(1401), 40) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "a key")
    real = git("rev-parse", ":k.env").stdout.decode().strip()
    (d / "benign.txt").write_text("DEPLOY_KEY=\n")
    benign = git("hash-object", "-w", "benign.txt").stdout.decode().strip()
    (d / "benign.txt").unlink()
    git("replace", "-f", real, benign)
    (d / "k.env").unlink()
    got = cs.scan_repo(d)
    refused = sorted({h["where"].split(":")[0] for h in got["credentials"]})
    check("the real index blob is read, not its replace stand-in", refused == ["k.env"], str(refused))


def test_round14_history_refuses_a_shallow_repository() -> None:
    """L2: a `.git/shallow` boundary at the commit that removed a key hid the commit
    that added it. The history mode refuses a shallow repository: incomplete, not clean."""
    import random
    cs = _cs()
    d, git = _repo("observatory-sec-shallow-")
    (d / "base.txt").write_text("base\n"); git("add", "-A"); git("commit", "-q", "-m", "base")
    (d / "k.env").write_text("DEPLOY_KEY=" + _secret_hex(random.Random(1402), 40) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "add key")
    (d / "k.env").unlink(); git("add", "-A"); git("commit", "-q", "-m", "remove key")
    head = git("rev-parse", "HEAD").stdout.decode().strip()
    (d / ".git" / "shallow").write_text(head + "\n")
    try:
        cs.history_log(d, 20)
    except cs.HistoryIncomplete as exc:
        check("a shallow repository's history is refused as incomplete", "shallow" in str(exc), str(exc))
    else:
        check("a shallow repository's history is refused as incomplete", False, "history_log returned")


def test_round14_skip_worktree_reads_the_index_not_the_working_copy() -> None:
    """L3: a skip-worktree entry is read from its index blob, even when a harmless file
    stands at its path in the working tree."""
    import random
    cs = _cs()
    d, git = _repo("observatory-sec-skip-")
    (d / "k.env").write_text("DEPLOY_KEY=" + _secret_hex(random.Random(1403), 40) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "a key")
    git("update-index", "--skip-worktree", "k.env")
    (d / "k.env").write_text("DEPLOY_KEY=\n")
    got = cs.scan_repo(d)
    refused = sorted({h["where"].split(":")[0] for h in got["credentials"]})
    check("a skip-worktree entry's index blob is judged, not the harmless working copy", refused == ["k.env"],
          str(refused))


def test_round14_path_branch_refuses_links() -> None:
    """H1: where a folder has no descriptor (Windows) the tree scan walks by path; the
    same rules hold there — exercised here on POSIX through the same function."""
    import random
    rng = random.Random(1404)
    cs = _cs()
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sec-bypath-")).resolve()
    outside = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sec-bypath-out-")).resolve()
    (outside / "k.env").write_text("DEPLOY_KEY=" + _secret_hex(rng, 40) + "\n")
    os.symlink(outside, d / "sub")
    (d / "real").mkdir(); (d / "real" / "a.txt").write_text("plain\n")
    os.symlink("target-text", d / "real" / "lnk")
    try:
        cs._tracked_bytes_by_path(d, ["sub", "k.env"])
    except cs._ParentLink as exc:
        check("a folder that is a link is refused on the path branch", exc.args[0] == "sub", str(exc.args))
    else:
        check("a folder that is a link is refused on the path branch", False, "read through the link")
    check("a regular file is read on the path branch", cs._tracked_bytes_by_path(d, ["real", "a.txt"]) == b"plain\n")
    check("a link is judged by its target text on the path branch",
          cs._tracked_bytes_by_path(d, ["real", "lnk"]) == b"target-text")


def test_the_exit_code_says_so() -> None:
    """Driven through the CLI, because the gate reads the exit code."""
    p = run_checker("--json")
    check("the shipped tree is clean", p.returncode == 0, (p.stdout + p.stderr)[-300:])
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        check("the checker answers in JSON", False, p.stdout[-200:] + p.stderr[-200:])
        return
    check("with no credential", not doc["credentials"], str(doc["credentials"])[:200])
    check("and no fixture left to argue about", not doc["fixtures"],
          str(doc["fixtures"])[:200])
    check("the floor is the measured one", doc["normalised_floor"] == 0.78,
          str(doc.get("normalised_floor")))


# ─────────── a name proves nothing ─────────────────────────────────────

def test_calling_a_real_key_fake_does_not_help() -> None:
    """The rule that makes the check worth having: entropy decides."""
    body = random_body(48)
    for name in ("FAKE", "FAKE_KEY", "TEST_ONLY_NOT_REAL", "example_key",
                 "dummy_token"):
        got = scan_string(f'{name} = "{SK}{body}"')
        check(f"a real key named {name} is still refused", bool(got["credentials"]),
              "if a word in the variable name could excuse a token, the check "
              "would be advice rather than a gate")
    got = scan_string(f'FAKE = "{SK}{body}"  # not a real key, honestly')
    check("nor does a comment saying so", bool(got["credentials"]))


def test_a_low_entropy_placeholder_is_not_a_credential() -> None:
    """The other side. A check that fails on documentation is one nobody runs."""
    placeholder = f'KEY = "{OR}your-key-goes-here-replace-this"'
    for text in (placeholder, f'KEY = "{SK}{"x" * 32}"', f'KEY = "{PHRASE_FIXTURE}"'):
        got = scan_string(text)
        check(f"{text[8:40]!r}… reads as a fixture", not got["credentials"],
              str(got["credentials"]))
    got = scan_string(placeholder)
    check("and it is REPORTED rather than ignored", bool(got["fixtures"]),
          "a fixture the scan never mentions is a fixture nobody can re-judge")


def test_the_prefix_is_stripped_before_measuring() -> None:
    """The OpenRouter prefix is fixed text every such key carries, so leaving it
    in dilutes the entropy of the part that is supposed to be random."""
    sys.path.insert(0, str(ROOT / "tools"))
    import check_secrets
    body = random_body(20)
    check("the vendor prefix is removed",
          check_secrets.body_of(OR + body) == body,
          check_secrets.body_of(OR + body))
    check("and a short body is not judged by entropy at all",
          check_secrets.MIN_BODY >= 16,
          "eight random characters score 3.0 no matter how random they are")


# ─────────── history, and the remedy ───────────────────────────────────

def test_history_is_a_separate_bounded_mode() -> None:
    p = run_checker("--history", "5", "--json")
    # Round 9: history paths are engine-relative now, so ALLOWLIST and SKIP apply
    # and the shipped history exits 0 rather than "runs".
    check("the history mode runs and exits 0", p.returncode == 0, (p.stdout + p.stderr)[-300:])
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        check("and answers in JSON", False, p.stdout[-200:])
        return
    check("and finds no credential in the last five commits",
          not doc["credentials"], str(doc["credentials"])[:200])
    src = (ROOT / "tools/check_secrets.py").read_text(encoding="utf-8")
    check("the remedy named is ROTATION, not a rewrite",
          "rotate the credential at its issuer" in src,
          "a rewrite breaks every clone and the credential is compromised either way")
    check("and the scope is the tracked tree, with the reason",
          "git ls-files" in src and "the operator's own working state" in src,
          "flagging untracked files is how a scanner becomes the one nobody runs")


# ─────────── the fixtures no longer have a shape ───────────────────────

def test_the_repository_holds_no_key_shaped_literal() -> None:
    """The tree scan skips THIS file, because it plants credentials; so this
    file is scanned here instead, and must hold nothing a scanner would flag.
    Every other file is covered by the tree scan above."""
    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    got = scan_string(src, "tests/test_secrets.py")
    check("this suite's own source holds no key-shaped literal",
          not got["credentials"], str(got["credentials"])[:200])
    check("and the composed phrase fixture is unchanged",
          PHRASE_FIXTURE == "".join(("sk", "-or-v1-a-fake-key-used-only-by-this-test")),
          PHRASE_FIXTURE)
    check("so every assertion that compares it still means the same thing",
          len(PHRASE_FIXTURE) == 42, str(len(PHRASE_FIXTURE)))


def test_the_check_is_in_the_gate() -> None:
    src = (ROOT / "observatory.py").read_text(encoding="utf-8")
    check("registered as a step", "check_secrets.py" in src)
    check("and inside the check group", '"secrets"' in src,
          "a secret scan outside the gate runs when somebody remembers it — "
          "which is the audit's own third finding, one level up")


if __name__ == "__main__":
    print("secrets — the claim, the check, and the false critical\n")
    for fn in (test_every_vendor_shape_is_caught, test_every_prefix_the_redactor_knows_is_caught,
               test_a_short_vendor_body_and_a_named_token_are_refused, test_a_word_before_a_prefix_is_not_an_issuer,
               test_a_known_prefix_with_its_body_is_always_refused, test_named_secrets_in_all_four_forms,
               test_round5_grammar_cases, test_round5_seeded_generator,
               test_round5_out_of_scope_is_documented_not_refused,
               test_round6_counterexamples, test_round6_files_are_read_as_bytes_and_an_unreadable_one_fails,
               test_round6_generator_crlf_and_non_utf8,
               test_round7_most_severe_verdict_wins_and_the_whole_value_is_measured,
               test_round7_generator_overlaps_bom_and_cr,
               test_round8_leading_underscores_and_the_cheap_lows, test_round8_history_is_read_as_bytes,
               test_round9_nested_names_slots_and_the_cheap_lows, test_round9_history_headers_are_a_state_machine,
               test_round9_tracked_names_survive_any_bytes,
               test_round10_history_reads_binary_marked_files_as_text, test_round10_history_diffs_merge_resolutions,
               test_round10_history_names_paths_whatever_the_prefix_config, test_round10_slot_names_as_wide_as_the_vault,
               test_round11_history_shows_the_root_commit, test_round11_history_ignores_replace_refs,
               test_round11_history_walks_side_branches, test_round11_history_quoted_name_with_a_space,
               test_round11_slot_name_at_most_128, test_round11_tree_scan_never_follows_links,
               test_round11_issuers_are_known_prefixes,
               test_round13_hex_values_are_refused_whatever_their_entropy, test_round13_history_ignores_grafts,
               test_round13_history_reads_renames_and_copies_as_content,
               test_round13_tree_scan_never_follows_a_parent_link, test_round13_gitlinks_skipped_and_index_entries_read,
               test_round13_large_file_is_unreadable, test_round13_text_output_gives_the_real_reason,
               test_round14_index_blob_ignores_replace_refs, test_round14_history_refuses_a_shallow_repository,
               test_round14_skip_worktree_reads_the_index_not_the_working_copy, test_round14_path_branch_refuses_links,
               test_the_exit_code_says_so,
               test_calling_a_real_key_fake_does_not_help,
               test_a_low_entropy_placeholder_is_not_a_credential,
               test_the_prefix_is_stripped_before_measuring,
               test_history_is_a_separate_bounded_mode,
               test_the_repository_holds_no_key_shaped_literal,
               test_the_check_is_in_the_gate):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma credential in the tree is now an exit code, and the fixture has no shape\033[0m")
