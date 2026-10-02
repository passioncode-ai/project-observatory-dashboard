#!/usr/bin/env python3
"""Installing a key must be safer than editing the file by hand.

Several consumers read a provider key from different files, and which one to
edit depends on which kind of key was made — and both kinds share one prefix.
`tools/install_key.py` takes the key on STDIN, asks the provider what it is,
and writes it mode 600 to the one place that consumer reads.

Everything here runs in a private temporary workspace, and the provider is a
stub installed inside the tool's own process: no request leaves the machine.
Most cases feed keys the stub refuses, because that is the property that
matters most — **a key that does not work must never reach a destination
file.** The accepted-key case is driven against the same stub, so the report a
person acts on is asserted without a real account.
"""
from __future__ import annotations
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                               # noqa: E402

PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
TOOL = ROOT / "tools/install_key.py"
FAILURES: list[str] = []

#: Synthetic key shapes. None is a credential anywhere.
REFUSED_KEY = "sk-or-v1-" + "0" * 40
ACCEPTED_KEY = "sk-or-v1-" + "a" * 40

#: Runs the real tool with `urllib.request.urlopen` replaced, so the tool's own
#: decision logic is what is exercised while no request can leave the process.
#: Every attempted URL is appended to a log, so "before any network call" is a
#: measurement rather than an inference from the message text.
DRIVER = r'''
import io, json, runpy, sys, urllib.error, urllib.request
calls, mode, tool = sys.argv[1], sys.argv[2], sys.argv[3]
class Reply(io.BytesIO):
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False
def stub(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    with open(calls, "a", encoding="utf-8") as f:
        f.write(url + "\n")
    if mode == "accept-inference":
        if url.endswith("/api/v1/keys"):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b""))
        return Reply(json.dumps({"data": {"limit": 5, "limit_remaining": 4.5,
                                          "limit_reset": "monthly"}}).encode())
    raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(b""))
urllib.request.urlopen = stub
sys.argv = [tool, *sys.argv[4:]]
runpy.run_path(tool, run_name="__main__")
'''


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def workspace() -> tuple[pathlib.Path, dict]:
    """A fresh, private workspace; nothing the machine already holds is visible."""
    base = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-installkey-")).resolve()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OBSERVATORY_", "CLAUDE_MEM_", "OPENAI_", "OPENROUTER_"))}
    env.update(HOME=str(base / "user"), OBSERVATORY_HOME=str(base / "home"),
               PYTHONDONTWRITEBYTECODE="1")
    (base / "user").mkdir()
    return base, env


def run(env: dict, base: pathlib.Path, stdin: str, *args: str,
        mode: str = "refuse") -> tuple[subprocess.CompletedProcess, list[str]]:
    calls = base / "calls.log"
    calls.write_text("", encoding="utf-8")
    driver = base / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    p = subprocess.run([PY, str(driver), str(calls), mode, str(TOOL), *args], input=stdin,
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    return p, [l for l in calls.read_text(encoding="utf-8").splitlines() if l]


def destinations(env: dict) -> dict:
    code = ("import json, runpy, sys\n"
            "sys.argv=['x']\n"
            f"ik = runpy.run_path({str(TOOL)!r}, run_name='not_main')\n"
            "print(json.dumps({'dest': {k: [str(p), w] for k, (p, w) in ik['DESTINATIONS'].items()},"
            " 'provisioning': str(ik['PROVISIONING'])}))\n")
    p = subprocess.run([PY, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=60)
    if p.returncode != 0:
        raise AssertionError("the tool could not be loaded: " + p.stderr[-300:])
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_a_key_in_an_argument_is_impossible() -> None:
    """The whole reason for stdin: argv lands in the history and the process list."""
    src = TOOL.read_text(encoding="utf-8")
    check("the tool declares no argument that could hold a key",
          "--key" not in src and "add_argument(\"key\"" not in src,
          "an argument named for a key is a key in `ps`")
    check("and it says why in the refusal a caller will see",
          "never in an argument" in src, "the reason has to travel with the rule")


def test_an_empty_or_malformed_key_is_refused_before_anything_is_written() -> None:
    base, env = workspace()
    p, calls = run(env, base, "")
    check("empty stdin is refused", p.returncode != 0 and "empty" in p.stderr, p.stderr[:120])
    p, calls = run(env, base, "hunter2", "--for", "claude-mem")
    check("a value that is not a provider key is refused",
          p.returncode != 0 and "sk-or-" in p.stderr, p.stderr[:120])
    check("and the refusal says what the value does not look like",
          "does not look like" in p.stderr, p.stderr[:120])
    check("and happens before any network call", calls == [], str(calls))
    # KEY-11: one prefix for both doors. `sk-or-x…` passed here and was then
    # refused by `full key`, which wants `sk-or-v1-`.
    p, calls = run(env, base, "sk-or-x" + "b" * 40, "--for", "observatory")
    check("a key the engine itself would refuse is refused here too",
          p.returncode != 0 and "sk-or-v1-" in p.stderr and calls == [], p.stderr[:160])
    src = TOOL.read_text(encoding="utf-8")
    check("the prefix is the providers' constant, not a second literal",
          "KEY_SHAPES" in src and '"sk-or-"' not in src, "")
    # KEY-10: a terminal on stdin names the consumer the caller chose.
    check("the stdin refusal echoes the chosen consumer", "--for {a.consumer or" in src, "")


def test_a_key_the_provider_rejects_never_reaches_a_file() -> None:
    """A 401 must stop the install, not be written and discovered later."""
    base, env = workspace()
    dest = pathlib.Path(destinations(env)["dest"]["claude-mem"][0])
    before = dest.read_bytes() if dest.is_file() else None
    p, calls = run(env, base, REFUSED_KEY, "--for", "claude-mem")
    check("the provider's refusal is the tool's refusal",
          p.returncode != 0 and "does not recognise" in p.stderr, p.stderr[:140])
    check("the provider was actually asked", bool(calls), "no stubbed call was recorded")
    after = dest.read_bytes() if dest.is_file() else None
    check("and the destination file is untouched", before == after,
          "a rejected key was written anyway")
    check("and the refusal never echoes the key", REFUSED_KEY not in p.stdout + p.stderr,
          "the value leaked into the output")


def test_every_destination_is_named_with_what_reads_it() -> None:
    """A list of paths teaches nothing; a path with its reader teaches the rule."""
    base, env = workspace()
    doc = destinations(env)
    dest = doc["dest"]
    check("both inference consumers are declared",
          set(dest) == {"observatory", "claude-mem"}, str(sorted(dest)))
    for name, (path, why) in dest.items():
        check(f"{name} names the file and its reader", len(why) > 20 and bool(path),
              f"{path}: {why}")
    # PORTED-DIVERGED: the engine keeps the key under the selected STATE of the
    # workspace, not under a fixed store directory of a checkout.
    check("the observatory's destination is the workspace state's key file",
          dest["observatory"][0] == str(base / "home/store/.openrouter-key"),
          dest["observatory"][0])
    check("the companion's destination is a .env file, the only file its worker reads",
          dest["claude-mem"][0].endswith("/.env"), dest["claude-mem"][0])
    revoke = (ROOT / "tools/revoke_key.py").read_text(encoding="utf-8")
    check("and a provisioning key has its own home, where revoke_key looks",
          doc["provisioning"] in revoke or "openrouter-provisioning" in revoke,
          doc["provisioning"])


def test_the_kind_is_asked_of_the_provider_not_read_off_the_prefix() -> None:
    """Both kinds are spelled the same; a rule on the string would be a guess."""
    src = TOOL.read_text(encoding="utf-8")
    check("the decision calls the provisioning endpoint",
          "/api/v1/keys" in src, "that endpoint is what only a provisioning key may use")
    check("and falls back to the key's own description",
          "/api/v1/key" in src)
    check("the reason is written where the next reader is",
          "would be a guess dressed as a check" in src or "NOT by the prefix" in src)


def test_a_dotenv_keeps_its_other_settings() -> None:
    """The companion's file carries more than a key; installing one must not truncate it."""
    base, env = workspace()
    d = base / "companion" / ".env"
    d.parent.mkdir()
    original = "KEEP_ME=1\nOPENROUTER_API_KEY=sk-or-v1-old\nALSO=2\n"
    d.write_text(original, encoding="utf-8")
    code = ("import runpy, sys, pathlib\n"
            "sys.argv=['x']\n"
            f"ik = runpy.run_path({str(TOOL)!r}, run_name='not_main')\n"
            f"ik['write'](pathlib.Path({str(d)!r}), 'sk-or-v1-' + 'n' * 20, 'claude-mem')\n")
    # PORTED-DIVERGED: the engine refuses to read a credential file that other
    # users can read, rather than silently merging into it. A group-readable
    # file is left exactly as it was.
    d.chmod(0o644)
    p = subprocess.run([PY, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=60)
    check("a group-readable existing file is refused", p.returncode != 0, p.stdout[-200:])
    check("and left byte for byte as it was", d.read_text(encoding="utf-8") == original)
    d.chmod(0o600)
    p = subprocess.run([PY, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=60)
    check("the writer runs on an owner-only file", p.returncode == 0, p.stderr[-200:])
    body = d.read_text(encoding="utf-8")
    check("the other settings survive", "KEEP_ME=1" in body and "ALSO=2" in body, body)
    check("the key line is replaced, not appended twice",
          body.count("OPENROUTER_API_KEY=") == 1, body)
    check("and the file is mode 600", oct(d.stat().st_mode & 0o777) == "0o600",
          oct(d.stat().st_mode & 0o777))


def test_an_accepted_key_reports_what_it_may_spend() -> None:
    """The report a person acts on, driven against the stubbed provider.

    The original drove this with a real key and skipped without one; with the
    provider stubbed in-process the property is covered on every run.
    """
    base, env = workspace()
    p, calls = run(env, base, ACCEPTED_KEY, "--for", "observatory", mode="accept-inference")
    check("an accepted inference key installs", p.returncode == 0, (p.stdout + p.stderr)[-200:])
    dest = base / "home/store/.openrouter-key"
    check("it lands in the observatory's destination",
          dest.is_file() and dest.read_text(encoding="utf-8") == ACCEPTED_KEY, str(dest))
    if dest.is_file():
        check("mode 600", oct(dest.stat().st_mode & 0o777) == "0o600",
              oct(dest.stat().st_mode & 0o777))
    check("the report names the limit and the reset",
          "limit" in p.stdout and "reset" in p.stdout, p.stdout[-160:])
    check("and never prints the key", ACCEPTED_KEY not in p.stdout + p.stderr, "the value leaked")
    # PORTED-DIVERGED: the engine prints the key's length and "value hidden"
    # rather than a visible tail, so not even a suffix of the value is shown.
    check("only its length, with the value hidden",
          f"length {len(ACCEPTED_KEY)}; value hidden" in p.stdout, p.stdout[:160])


if __name__ == "__main__":
    print("installing a key — one paste, the right file, nothing printed\n")
    for fn in (test_a_key_in_an_argument_is_impossible,
               test_an_empty_or_malformed_key_is_refused_before_anything_is_written,
               test_a_key_the_provider_rejects_never_reaches_a_file,
               test_every_destination_is_named_with_what_reads_it,
               test_the_kind_is_asked_of_the_provider_not_read_off_the_prefix,
               test_a_dotenv_keeps_its_other_settings,
               test_an_accepted_key_reports_what_it_may_spend):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma key reaches its consumer and nothing else\033[0m")
