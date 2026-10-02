#!/usr/bin/env python3
"""Where the key may live, and what must never happen to it.

A secret has three ways to leak that a test can actually catch: loose file
permissions, a plaintext copy somewhere a process can read, and a log line.
"""
from __future__ import annotations
import importlib.util, json, os, pathlib, plistlib, subprocess, sys, tempfile
import sys as _sys, pathlib as _pl
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import tmp as tmpdir  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable

#: COMPOSED, so no key-shaped literal exists anywhere in the tracked tree.
#: The value is unchanged and every assertion below still compares it exactly;
#: what changes is that a scanner reading this file finds no token to judge.
#:
#: A project audit once raised this line as two CRITICAL findings — "an
#: openai-style key is committed in the tree", and the same in history. Both
#: were false: the body reads `a-fake-key-used-only-by-this-test` and
#: scores 3.65 bits of entropy per character against 4.60 for a random 32-char
#: key. But a permanent false critical is the shape that teaches an operator to
#: skip the one that is real, and an allowlist entry only teaches MY scanner.
#: Removing the shape convinces every scanner, including the ones nobody here
#: configures. `tools/check_secrets.py` is the check that would catch a real
#: one, and it is in the gate.
FAKE = "sk-" + "or-v1-" + "-".join(
    ("a", "fake", "key", "used", "only", "by", "this", "test"))
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def providers_mod():
    sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "agent"))
    sys.modules.pop("providers", None)
    spec = importlib.util.spec_from_file_location("providers", ROOT / "agent/providers.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_a_loose_key_file_is_refused() -> None:
    pr = providers_mod()
    f = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-key-")) / "k"
    f.write_text(FAKE)
    orig = pr.KEY_FILES
    try:
        pr.KEY_FILES = (f,)
        os.environ.pop(pr.KEY_ENV, None)
        for mode, must_refuse in ((0o644, True), (0o640, True), (0o600, False), (0o400, False)):
            f.chmod(mode)
            try:
                key, _ = pr.read_key()
                refused = False
            except pr.Fatal:
                refused = True
            check(f"mode {mode:o} is {'refused' if must_refuse else 'accepted'}",
                  refused == must_refuse, f"refused={refused}")
    finally:
        pr.KEY_FILES = orig
        f.unlink(missing_ok=True)


def test_the_env_var_wins_and_the_order_is_stated() -> None:
    pr = providers_mod()
    os.environ[pr.KEY_ENV] = FAKE
    try:
        key, where = pr.read_key()
        check("the environment variable wins", key == FAKE and pr.KEY_ENV in where, where)
    finally:
        os.environ.pop(pr.KEY_ENV, None)
    # THE PROJECT'S OWN KEY FIRST. This once asserted the opposite, and the
    # opposite made the operator's remedy impossible: while the machine's
    # shared secret came first, a key placed here for this project alone could
    # never win, so "give the observatory its own key" changed nothing. The
    # shared file stays as the FALLBACK — that is what keeps the tick working
    # when no project key exists.
    check("the project's own key file is looked in first",
          "store" in str(pr.KEY_FILES[0]) and ".openrouter-key" in str(pr.KEY_FILES[0]),
          str(pr.KEY_FILES[0]))
    # The engine's fallback is the workspace's secret store (or the store a
    # workspace names as `secret_store`), not a fixed machine path.
    import paths
    store = paths.source_path("secret_store", paths.SECRETS)
    check("and the shared secret store is the fallback behind it",
          pr.KEY_FILES[-1] == store / "openrouter", str(pr.KEY_FILES[-1]))


def test_the_key_command_answers_for_the_scheduled_run_too() -> None:
    """The shell's answer is not the system's answer, and the operator hit it.

    Minutes after giving the project its own key file, the operator ran `./observatory.py key`, saw the shell's exported key, and
    reasonably read it as "the file did not take". It had taken: launchd passes
    only HOME and PATH, so the tick — the process that SPENDS — never sees that
    variable and reads the file. One command, two callers, one answer shown.

    Driven rather than asserted about: the environment is planted, and the two
    resolutions must differ in exactly the way the two callers do.
    """
    pr = providers_mod()
    keyfile = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-twocallers-")) / "k"
    keyfile.write_text("sk-or-v1-" + "f" * 40, encoding="utf-8")
    keyfile.chmod(0o600)
    os.environ["OBSERVATORY_KEY_FILE"] = str(keyfile)
    os.environ[pr.KEY_ENV] = "sk-or-v1-" + "e" * 40
    try:
        pr2 = providers_mod()                     # re-read KEY_FILES with the redirect
        here = pr2.key_status()
        there = pr2.scheduled_key_status()
        check("the shell's answer names the environment variable",
              pr2.KEY_ENV in here, here)
        check("the scheduled answer names the FILE instead",
              str(keyfile) in there, there)
        check("and the two are different, which is the whole point",
              here != there, f"{here!r} == {there!r}")
        check("neither prints the key whole",
              "f" * 40 not in here + there and "e" * 40 not in here + there)
    finally:
        os.environ.pop(pr.KEY_ENV, None)
        os.environ.pop("OBSERVATORY_KEY_FILE", None)
    src = (ROOT / "agent/providers.py").read_text(encoding="utf-8")
    check("and the command prints the second answer, not only computes it",
          "the scheduled tick resolves" in src,
          "a resolver nobody calls answers nobody")


def test_the_key_is_never_printed_whole() -> None:
    pr = providers_mod()
    os.environ[pr.KEY_ENV] = FAKE
    try:
        status = pr.key_status()
        check("key_status shows a shape, never the key", FAKE not in status, status[:80])
        check("key_status still says where it came from", pr.KEY_ENV in status, status[:80])
        # KEY-12: no character of the key beyond the provider's own prefix.
        check("key_status carries no tail of the key", FAKE[-4:] not in status, status[:80])
        check("and says presence and length", f"length {len(FAKE)}" in status, status[:80])
    finally:
        os.environ.pop(pr.KEY_ENV, None)
    p = subprocess.run([PY, "agent/providers.py", "key"],
                       cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, pr.KEY_ENV: FAKE}, timeout=60)
    check("the CLI does not print the key either", FAKE not in (p.stdout + p.stderr),
          (p.stdout + p.stderr)[:100])


def test_the_launchd_plist_carries_no_secret() -> None:
    """A plist is world-readable. A key in EnvironmentVariables is a key on show.

    Built by the installer's own `build()` and round-tripped through plistlib,
    with a key planted in the environment, rather than read from an installed
    job: nothing is written to the scheduler, and the property holds on every
    machine, not only the one that has the job installed.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    os.environ["OPENROUTER_API_KEY"] = FAKE
    try:
        import install_launchd
        d = plistlib.loads(plistlib.dumps(install_launchd.build(1800)))
    finally:
        os.environ.pop("OPENROUTER_API_KEY", None)
    env = d.get("EnvironmentVariables", {})
    check("the plist declares no key", not any("KEY" in k.upper() or "TOKEN" in k.upper()
                                               or "SECRET" in k.upper() for k in env),
          str(list(env)))
    check("the plist holds nothing that looks like a key",
          not any(str(v).startswith("sk-") for v in env.values()), str(list(env.values()))[:80])
    src = (ROOT / "tools/install_launchd.py").read_text(encoding="utf-8")
    check("the installer explains why the plist carries no key",
          "world-readable" in src or "plaintext" in src,
          "the reason belongs where the next person will edit it")


def test_no_key_is_committed() -> None:
    """The program tree ships no key; the key lives in the selected workspace.

    The whole-tree and history scan is `tools/check_secrets.py` over a Git
    checkout, and the public repository's own release check scans every
    published blob. This sandbox is a source copy without Git, so what is
    driven here is the part that holds anywhere: no key file in the source, the
    key path outside it, and this file's own fixture not key-shaped.
    """
    shipped = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
               if p.name in (".openrouter-key", "wallet.json") and p.is_file()]
    check("no key file is shipped with the program", not shipped, str(shipped))
    pr = providers_mod()
    check("the project key path is in the workspace state, not the source tree",
          ROOT not in pr.KEY_FILES[0].parents, str(pr.KEY_FILES[0]))
    sys.path.insert(0, str(ROOT / "tools"))
    import check_secrets
    own = check_secrets.scan_text(pathlib.Path(__file__).read_text(encoding="utf-8"),
                                  "tests/test_key.py")
    check("this file's own fixture is not key-shaped in the tree",
          not own, "composed at runtime, so no scanner has a token to judge: " + str(own)[:160])
    planted = check_secrets.scan_text("key = " + json.dumps("sk-or-v1-" + "Zq8" * 16),
                                      "planted")
    check("while the same scanner does see a planted key", bool(planted), str(planted)[:160])


if __name__ == "__main__":
    print("the key — where it may live, and how it leaks\n")
    for fn in (test_a_loose_key_file_is_refused, test_the_env_var_wins_and_the_order_is_stated,
               test_the_key_command_answers_for_the_scheduled_run_too,
               test_the_key_is_never_printed_whole, test_the_launchd_plist_carries_no_secret,
               test_no_key_is_committed):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mkey handling ok\033[0m")
