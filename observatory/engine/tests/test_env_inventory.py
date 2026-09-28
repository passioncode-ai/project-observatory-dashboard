#!/usr/bin/env python3
"""The env inventory, tested where it is DANGEROUS rather than where it is easy.

Three properties carry this feature, and every one of them is a property about
something NOT happening:

  the value never leaves     the scan reads it, classifies it, drops it. What
                             survives is one word and, for a secret, a salted
                             fingerprint — and the fingerprint reaches the
                             gitignored scan, never the registry.
  the classifier is honest   in both directions. A rule that calls everything a
                             secret is useless in the same way as one that calls
                             nothing a secret, and the first version of this
                             classifier did the former: over a hundred committed
                             `.env.example` templates reported as live keys,
                             because `^your[-_ ]?$` does not match
                             `your-gemini-api-key` and the NAME rule then fired.
                             Both directions are driven below with the exact
                             values that were misread.
  the reveal stays narrow    it resolves the request against the INVENTORY, not
                             against the path in the body, so it is a reader of
                             a measured list rather than a file reader holding a
                             token. Driven over a real socket, and the
                             traversal attempt is one of the cases.

The tab itself is driven by `tests/env_tab_check.js`, which executes the built
page's own script — registered as its own gate step because it needs node.
"""
from __future__ import annotations
import json, os, pathlib, socket, sys, threading, time
import urllib.error, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
from test_portable_mcp import setup as portable_setup  # noqa: E402
portable_setup()
import tmp as tmpdir  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


# ── the classifier, in both directions ─────────────────────────────────────

#: Values that ARE credentials. Composed rather than written where a real issuer
#: prefix is involved — `tools/check_secrets.py` reads this file on every gate
#: run and cannot tell a fixture from a live key, which is the correct default.
def _k(prefix: str, body: str) -> str:
    return prefix + body


LIVE_VALUES = [
    ("OPENAI_API_KEY", _k("sk-", "proj_" + "b7Qn4xLm2Zt9Rk1VwYc8Hd3Fg6Js0Pa5")),
    ("GIT_HOST_TOKEN", _k("ghp_", "K2mNv8QrT4xZ1bLc7YdW3aJf6Hs9Pe0Ug5Rt")),
    ("DATABASE_URL", "postgres://svc:9fK2mQvXzR4t@db.internal:5432/main"),
    ("SESSION_SECRET", "7d41f0bc9a2e5183c6b740fe29ad5c83"),
    ("CLOUD_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    ("JWT", _k("eyJ", "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSJ9")),
]

#: Values that are NOT, and every one of them was misread by a version of this
#: classifier that shipped.
NOT_SECRETS = [
    ("GEMINI_API_KEY", "your-gemini-api-key", "placeholder",
     "the lead-in was anchored at both ends, so this matched nothing"),
    ("GEMINI_MODEL", "gemini-2.5-flash-preview", "config",
     "length plus entropy called a model name a credential"),
    ("VIDEO_RENDER_KEY", "any-string", "placeholder",
     "the NAME rule outvoted a value that reads as words"),
    ("DATABASE_URL", "postgres://user:password@localhost:5432/mydb", "config",
     "an example password is not a credential"),
    ("SIGNIN_OAUTH_REDIRECT_URL", "http://localhost:3000/auth/return", "config",
     "`OAUTH` contains `AUTH`, and an address is not a credential"),
    ("DB_PASSWORD", "%env(urlencode:DB_PASSWORD)%", "placeholder",
     "a framework substitution is a slot, not a value"),
    ("APP_ENV", "production", "config", ""),
    ("PORT", "3000", "config", ""),
    ("STRIPE_SECRET_KEY", "", "empty", ""),
]


def test_a_credential_is_recognised_by_shape():
    import scan_env
    for name, value in LIVE_VALUES:
        got = scan_env.classify(name, value)
        check(f"{name} reads as a secret", got == "secret", f"got {got!r}")


def test_what_is_not_a_credential_is_not_called_one():
    """Every row here was a false positive that shipped. They stay as fixtures."""
    import scan_env
    for name, value, want, why in NOT_SECRETS:
        got = scan_env.classify(name, value)
        check(f"{name}={value[:28]!r} is {want}", got == want,
              f"got {got!r}" + (f" — {why}" if why else ""))


def test_a_comment_is_not_a_value_end_to_end():
    """The defect was in the PARSER, so the assertion has to run the pair.

    `VOICE_API_KEY=  # Voice service API key for speech generation` became
    an English sentence, and an English sentence has the entropy of a
    credential — which reported dozens of committed templates as live keys. Asserting
    it against `classify` alone would pass for the wrong reason: `classify` never
    sees a comment on the working path.
    """
    import scan_env
    pairs, _ = scan_env.parse(
        "VOICE_API_KEY=  # Voice service API key for speech generation\n")
    name, value = pairs[0]
    check("the comment is stripped by the parser", value == "", repr(value))
    check("so the variable classifies as empty rather than as a secret",
          scan_env.classify(name, value) == "empty",
          scan_env.classify(name, value))


def test_the_parser_keeps_a_value_whole():
    import scan_env
    text = "\n".join([
        "# a comment",
        "",
        "PLAIN=one",
        "export EXPORTED=two",
        'QUOTED="three four"',
        "TRAILING=five # a note",
        "BLANK=  # only a note",
        'PEM="-----BEGIN KEY-----',
        "line-two",
        'line-three-----END KEY-----"',
        "AFTER=six",
    ])
    pairs, unparsed = scan_env.parse(text)
    got = dict(pairs)
    check("a plain assignment is read", got.get("PLAIN") == "one", repr(got.get("PLAIN")))
    check("`export` is stripped", got.get("EXPORTED") == "two", repr(got.get("EXPORTED")))
    check("quotes are stripped", got.get("QUOTED") == "three four", repr(got.get("QUOTED")))
    check("a trailing comment is not part of the value",
          got.get("TRAILING") == "five", repr(got.get("TRAILING")))
    check("a value that is only a comment is empty",
          got.get("BLANK") == "", repr(got.get("BLANK")))
    # A PEM IS ONE VARIABLE. Counting its lines as unparsable would report a
    # healthy file as damaged, and a private key is exactly the value most worth
    # getting right.
    check("a quoted value may span lines",
          (got.get("PEM") or "").count("\n") == 2, repr((got.get("PEM") or "")[:20]))
    check("parsing resumes after it", got.get("AFTER") == "six", repr(got.get("AFTER")))
    check("nothing legitimate is counted unparsed", unparsed == 0, str(unparsed))


def isolated_scan(root):
    import tempfile
    from unittest.mock import patch
    import scan_env
    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory) / '.env-fingerprint-salt'
        path.write_text('a' * 64 + '\n')
        path.chmod(0o600)
        with patch.object(scan_env, 'SALT_FILE', path):
            return scan_env.scan(root)


def test_only_a_secret_is_fingerprinted():
    """`APP_ENV=production` shared by forty projects is not a shared credential."""
    import scan_env
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-env-"))
    (d / "proj").mkdir()
    (d / "proj" / ".env").write_text(
        "APP_ENV=production\nPORT=3000\n"
        f"API_KEY={LIVE_VALUES[3][1]}\n", encoding="utf-8")
    doc = isolated_scan(d)
    variables = {v["name"]: v for f in doc["files"] for v in f["variables"]}
    check("a secret carries a fingerprint", "fingerprint" in variables["API_KEY"])
    check("configuration does not", "fingerprint" not in variables["APP_ENV"],
          str(variables["APP_ENV"]))
    check("and neither does a port", "fingerprint" not in variables["PORT"])
    check("no value appears anywhere in the scan",
          LIVE_VALUES[3][1] not in json.dumps(doc),
          "the scan carries the value it was asked to describe")


def test_the_document_carries_no_fingerprint():
    """The scan may hold one; the REGISTRY is in git and may not."""
    import env_registry, scan_env
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-env-"))
    for proj in ("alpha", "beta"):
        (d / proj).mkdir()
        (d / proj / ".env").write_text(f"SHARED_TOKEN={LIVE_VALUES[4][1]}\n",
                                       encoding="utf-8")
    scan = isolated_scan(d)
    doc = env_registry.document(scan, "2026-09-12")
    text = json.dumps(doc)
    check("no fingerprint reaches the document", '"fingerprint"' not in text)
    check("no value reaches it either", LIVE_VALUES[4][1] not in text)
    # THE WHOLE POINT OF THE FINGERPRINT, and it survives the fingerprint being
    # dropped: the CONCLUSION is what the registry keeps.
    v = doc["files"][0]["variables"][0]
    check("but the conclusion does — the other project is named",
          v.get("shared_with") == ["beta"] or v.get("shared_with") == ["alpha"],
          str(v))
    check("and the shared group is one row, not two",
          len(doc["shared"]) == 1, str(len(doc["shared"])))


def test_a_template_offers_nothing_to_fill_from():
    """A `.env.example` is a DECLARATION. Offering it as a source is a circle."""
    import env_registry, scan_env
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-env-"))
    (d / "has").mkdir()
    (d / "has" / ".env").write_text(f"STRIPE_SECRET_KEY={LIVE_VALUES[0][1]}\n",
                                    encoding="utf-8")
    (d / "wants").mkdir()
    (d / "wants" / ".env").write_text("STRIPE_SECRET_KEY=\n", encoding="utf-8")
    (d / "wants" / ".env.example").write_text("STRIPE_SECRET_KEY=\n", encoding="utf-8")
    doc = env_registry.document(isolated_scan(d), "2026-09-12")
    by_path = {f["path"]: f for f in doc["files"]}
    live = by_path["wants/.env"]["variables"][0]
    tpl = by_path["wants/.env.example"]["variables"][0]
    check("an empty slot in a live file names where a value exists",
          live.get("available_in") == ["has"], str(live))
    check("the same slot in a template does not",
          "available_in" not in tpl, str(tpl))
    check("and the template is marked as one",
          by_path["wants/.env.example"]["kind"] == "template")


def test_the_git_state_tells_ignored_from_merely_untracked():
    """`loose` is the state that looks safe in every listing that does not ask."""
    import scan_env
    import subprocess
    d = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-env-"))
    repo = d / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(d / "gitconfig"),
           "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / ".gitignore").write_text(".env.ignored\n", encoding="utf-8")
    (repo / ".env.ignored").write_text("A=1\n", encoding="utf-8")
    (repo / ".env.loose").write_text("B=2\n", encoding="utf-8")
    (repo / ".env.tracked").write_text("C=3\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-f", ".env.tracked", ".gitignore"],
                   check=True, env=env)
    doc = isolated_scan(d)
    state = {f["path"].split("/")[-1]: f["git"] for f in doc["files"]}
    check("an ignored file says ignored", state.get(".env.ignored") == "ignored", str(state))
    check("a tracked file says tracked", state.get(".env.tracked") == "tracked", str(state))
    check("and untracked-but-unignored is its own answer",
          state.get(".env.loose") == "loose", str(state))


# ── the finding rules ──────────────────────────────────────────────────────

def _doc(files: list[dict], shared: list[dict] | None = None) -> dict:
    return {"files": files, "shared": shared or [], "totals": {}}


def _file(path: str, **kw) -> dict:
    base = {"id": "env:" + path, "path": path, "project": path.split("/")[0],
            "kind": "env", "git": "ignored", "mode": "0600",
            "modified_on": "2026-09-01", "unparsed_lines": 0, "counts": {},
            "variables": [{"name": "TOKEN", "class": "secret"}]}
    base.update(kw)
    return base


def test_a_committed_template_is_not_an_incident():
    """168 of them are committed on purpose. A rule that cannot tell is noise."""
    import env_findings
    rows = env_findings.tracked_in_git(_doc([
        _file("a/.env.example", kind="template", git="tracked"),
        _file("b/.env", git="tracked"),
    ]))
    check("only the live file is reported", len(rows) == 1, str(len(rows)))
    check("and it is the live one", rows and rows[0]["subject"] == "env:b/.env",
          str(rows))
    check("holding a credential makes it critical",
          rows and rows[0]["severity"] == "critical", str(rows))
    check("under the name the board carries",
          rows and rows[0]["type"] == "env.tracked_in_git", str(rows))


def test_a_tracked_file_with_no_credential_is_a_warning_not_an_incident():
    import env_findings
    rows = env_findings.tracked_in_git(_doc([
        _file("b/.env", git="tracked",
              variables=[{"name": "PORT", "class": "config"}])]))
    check("severity drops when nothing in it is a credential",
          rows and rows[0]["severity"] == "warning", str(rows))


def test_the_mode_rule_ignores_a_file_with_nothing_to_protect():
    import env_findings
    quiet = env_findings.world_readable(_doc([
        _file("a/.env", mode="0644", variables=[{"name": "PORT", "class": "config"}])]))
    loud = env_findings.world_readable(_doc([_file("b/.env", mode="0644")]))
    check("a world-readable file of configuration raises nothing", not quiet, str(quiet))
    check("a world-readable file of credentials does", len(loud) == 1, str(loud))
    check("under the name the board carries",
          loud and loud[0]["type"] == "env.world_readable", str(loud))
    check("as ONE row, because the remedy is one loop",
          len(env_findings.world_readable(_doc([
              _file("a/.env", mode="0644"), _file("b/.env", mode="0666")]))) == 1)


def test_the_shared_rule_points_at_curation_rather_than_replacing_it():
    import env_findings
    rows = env_findings.shared_secret(_doc([], [
        {"projects": ["a", "b", "c"], "names": ["API_HASH", "TELEGRAM_API_HASH"],
         "sites": ["a/.env:API_HASH"], "class": "secret", "in_templates": 0},
        {"projects": ["d", "e"], "names": ["X"], "sites": [], "class": "config",
         "in_templates": 0},
    ]))
    check("a configuration group raises nothing", len(rows) == 1, str(len(rows)))
    check("under the name the board carries",
          rows and rows[0]["type"] == "env.shared_secret", str(rows))
    check("the row names the curation file rather than claiming the decision",
          rows and "credential_owners.json" in rows[0]["action"], str(rows))
    check("and says equality is not intent",
          rows and "not intent" in rows[0]["action"], str(rows))


def test_an_empty_slot_with_a_live_twin_says_where_to_look():
    """`available_in` is an offer to look, never a licence to copy — a staging
    key and a production key share a name by design, and the rule must say so."""
    import env_findings
    rows = env_findings.reusable_slot(_doc([
        _file("a/.env", variables=[
            {"name": "STRIPE_KEY", "class": "empty", "available_in": ["b/.env"]}]),
        _file("c/.env"),
    ]))
    check("one aggregated row, under the name the board carries",
          len(rows) == 1 and rows[0]["type"] == "env.reusable_slot", str(rows))
    check("at info, because it is an offer rather than a fault",
          rows and rows[0]["severity"] == "info", str(rows))
    check("and it says where to look, not what to copy",
          rows and "not what to copy" in rows[0]["detail"], str(rows)[:200])
    check("no empty slot, no row",
          env_findings.reusable_slot(_doc([_file("c/.env")])) == [])


def test_nothing_scanned_raises_nothing():
    """Absent is not clean, and the rules say nothing rather than saying clean."""
    import env_findings
    check("no document, no rows", env_findings.findings(None) == [])
    check("an empty document, no rows", env_findings.findings({"files": []}) == [])


# ── the reveal, over a real socket ─────────────────────────────────────────

TOKEN = "-".join(("another", "token", "only", "this", "test", "knows"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def post(port: int, path: str, body: dict, token: str | None = TOKEN):
    h = {"Content-Type": "application/json"}
    if token:
        h["X-Observatory-Token"] = token
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(), headers=h,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:160]}


def test_the_reveal_resolves_against_the_inventory_not_the_request():
    """The difference between a reader of a list and a file reader with a token."""
    import subprocess
    import keyserver
    # PLANTED, so the case runs everywhere: one synthetic project with an env
    # file, scanned by the real collector into the workspace's scratch.
    project = keyserver.paths.DATA / "sample-0"
    (project / ".env").write_text("PORT=3000\nDEMO_TOKEN=" + "Qx7" * 12 + "\n",
                                  encoding="utf-8")
    scan = keyserver.paths.SCRATCH / "env.json"
    PY = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
    p = subprocess.run([PY, "collectors/scan_env.py", str(scan)], cwd=ROOT,
                       capture_output=True, text=True, timeout=300)
    check("the synthetic estate is scanned", p.returncode == 0 and scan.is_file(),
          (p.stdout + p.stderr)[-300:])
    if not scan.is_file():
        return
    port = free_port()
    # THE AUDIT GOES TO THIS TEST'S OWN FILE. Every reveal below once landed in
    # the workspace's `store/logs/keyserver.jsonl`, so the security record could
    # not tell a gate run from a person. The gate now hashes the journals too.
    import tmp as tmpdir
    audit_was = keyserver.AUDIT
    keyserver.AUDIT = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-reveal-")) / "keyserver.jsonl"
    live = keyserver.paths.STATE / "logs" / "keyserver.jsonl"
    live_before = live.read_bytes() if live.is_file() else b""
    srv = keyserver.Server(("127.0.0.1", port), keyserver.Handler, TOKEN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.15)
    try:
        code, d = post(port, "/api/reveal", {"path": "x", "name": "Y"}, token=None)
        check("no token is 401", code == 401, f"{code} {d}")

        code, d = post(port, "/api/reveal", {"path": "../../../etc/passwd", "name": "root"})
        check("a path outside the inventory is refused", code == 400, f"{code} {d}")
        check("and the refusal says it is about the inventory, not the filesystem",
              "inventory" in json.dumps(d), json.dumps(d)[:140])

        doc = json.loads(scan.read_text(encoding="utf-8"))
        real = next((f for f in doc["files"] if f["variables"]), None)
        check("the scan holds a variable to name", real is not None, str(doc)[:200])
        if real is None:
            return
        code, d = post(port, "/api/reveal",
                       {"path": real["path"], "name": "NO_SUCH_VARIABLE_HERE"})
        check("a variable the scan does not list is refused", code == 400, f"{code} {d}")

        code, d = post(port, "/api/reveal", {"path": real["path"], "name": ""})
        check("an empty name is refused rather than guessed", code == 400, f"{code} {d}")

        # THE ONE SUCCESS PATH, and the assertion is about SHAPE: printing the
        # value would put it in a transcript, which is the exact failure the
        # leak register exists to record.
        name = real["variables"][0]["name"]
        code, d = post(port, "/api/reveal", {"path": real["path"], "name": name})
        check("a variable the scan DOES list is returned",
              code == 200 and isinstance(d.get("value"), str), f"{code} {list(d)}")
        check("and the answer names what it answered about",
              d.get("name") == name and d.get("path") == real["path"], str(list(d)))
        rows = [json.loads(l) for l in keyserver.AUDIT.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("the reveal was journalled, in this test's own file, with its caller",
              any(r["action"] == "reveal" and r.get("caller") == "unnamed" for r in rows),
              str(rows[-1:] )[:200])
    finally:
        srv.shutdown()
        keyserver.AUDIT = audit_was
        live_after = live.read_bytes() if live.is_file() else b""
        check("and the LIVE journal is byte-identical to before this test",
              live_before == live_after,
              "a suite that writes the security record is a record nobody can read")


def test_the_audit_line_precedes_the_read():
    """A log written after the read loses the case where the read is the problem."""
    src = (ROOT / "tools/keyserver.py").read_text(encoding="utf-8")
    body = src.split("def act_reveal(", 1)[1].split("\ndef ", 1)[0]
    i_audit = body.find("audit(")
    # THE RIGHT READ. The first `read_text(` in this function opens the SCAN,
    # which is an inventory and not a secret; the one that matters is the env
    # file. Naming it is the difference between an assertion and a coincidence —
    # the first version of this check found the scan and failed on a function
    # that is in fact correct.
    i_read = body.find("target.read_text(")
    check("act_reveal writes an audit line", i_audit >= 0)
    check("and writes it before it opens the file", 0 <= i_audit < i_read,
          f"audit at {i_audit}, read at {i_read}")
    check("the audit line carries the class, never the value",
          '"class"' in body and '"value": v' in body.split("audit(")[1],
          "the value is returned but must not be logged")


if __name__ == "__main__":
    print("the env inventory — names, never values\n")
    for fn in (test_a_credential_is_recognised_by_shape,
               test_what_is_not_a_credential_is_not_called_one,
               test_a_comment_is_not_a_value_end_to_end,
               test_the_parser_keeps_a_value_whole,
               test_only_a_secret_is_fingerprinted,
               test_the_document_carries_no_fingerprint,
               test_a_template_offers_nothing_to_fill_from,
               test_the_git_state_tells_ignored_from_merely_untracked,
               test_a_committed_template_is_not_an_incident,
               test_a_tracked_file_with_no_credential_is_a_warning_not_an_incident,
               test_the_mode_rule_ignores_a_file_with_nothing_to_protect,
               test_the_shared_rule_points_at_curation_rather_than_replacing_it,
               test_an_empty_slot_with_a_live_twin_says_where_to_look,
               test_nothing_scanned_raises_nothing,
               test_the_reveal_resolves_against_the_inventory_not_the_request,
               test_the_audit_line_precedes_the_read):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32mthe value is read, classified and dropped\033[0m")
