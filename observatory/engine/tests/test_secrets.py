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
    }
    for label, text in planted.items():
        got = scan_string(text)
        check(f"a planted {label} is refused", bool(got["credentials"]),
              f"{got} — a scanner that misses this is a green nobody earned")


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
    check("the history mode runs", p.returncode in (0, 1), p.stderr[-200:])
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
    for fn in (test_every_vendor_shape_is_caught,
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
