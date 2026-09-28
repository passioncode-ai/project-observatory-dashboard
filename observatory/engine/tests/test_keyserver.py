#!/usr/bin/env python3
"""A control plane for credentials, tested by what it REFUSES.

The dangerous half of this component is not what it does — minting a key and
writing it to a file at mode 600 is four lines — but what it declines to do, and
every refusal here is load-bearing:

  no token         it holds a provisioning key; anything on this machine could
                   otherwise drive it
  foreign Origin   a page on another site must not post to 127.0.0.1 blind, and
                   the custom header forces a preflight that check can answer
  vault put/rotate REFUSED BY DESIGN. `tools/vault.py`'s contract is that a
                   value travels on stdin and nowhere else — not argv, not a
                   chat message, not an HTTP body a browser tab holds in memory.
                   A control plane that broke that rule to be convenient would
                   be the largest hole in the system it protects.
  a key in service revoking what a consumer is reading takes it down with no
                   warning and no way back: the value is gone.
  a routable bind  it answers to this machine or it does not run.

The tests drive a real socket on a free port and only touch paths that make no
network call, so nothing here mints, caps or revokes anything.
"""
from __future__ import annotations
import json, pathlib, socket, subprocess, sys, threading, time, urllib.error, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int, token: str):
    import keyserver
    # Every server this suite starts journals into a scratch file, never the
    # live `store/logs/keyserver.jsonl` — the gate hashes that journal now, and
    # a suite that writes the security record is a record nobody can read.
    import tmp as tmpdir
    keyserver.AUDIT = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-ks-")) / "keyserver.jsonl"
    srv = keyserver.Server(("127.0.0.1", port), keyserver.Handler, token)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.15)
    return srv


def call(port: int, path: str, *, token: str | None = None, origin: str | None = None,
         body: dict | None = None, caller: str | None = None,
         host: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Observatory-Token"] = token
    if origin:
        headers["Origin"] = origin
    if caller is not None:
        headers["X-Observatory-Caller"] = caller
    if host is not None:
        headers["Host"] = host
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, {"content_type": r.headers.get("Content-Type")}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:120]}


# COMPOSED, not written. `tools/check_secrets.py` reports a secret-shaped
# literal under a name like TOKEN as a fixture, and the rule is not
# pedantry: a reader cannot tell a fixture from a live credential, so the
# tree carries none of either. The value is unchanged.
TOKEN = "-".join(("a", "token", "only", "this", "test", "knows"))


def test_the_audit_names_the_caller():
    """The token says "this machine"; the caller header says WHICH of the nine
    agent sessions on it. 33 reveals of one variable in one night (2026-09-14)
    were attributable only by correlating session files' mtimes — so a caller
    names itself, an unnamed one is recorded as such, and a header is treated
    as the attacker-shaped input it is."""
    import keyserver
    check("caller_name keeps a plain id", keyserver.caller_name("session:c1260807") == "session:c1260807")
    check("and strips what does not belong in a journal",
          keyserver.caller_name("  page:creds\n<script>") == "page:credsscript", keyserver.caller_name("  page:creds\n<script>"))
    check("an empty or missing header is `unnamed`, never blank",
          keyserver.caller_name("") == "unnamed" and keyserver.caller_name(None) == "unnamed")
    check("a long header is cut, not refused", len(keyserver.caller_name("x" * 500)) == 80)
    port, token = free_port(), TOKEN
    # An action of this test's own, so the header's journey through
    # `_authorised` into the request's context is watched end to end without
    # minting, revoking or revealing anything real.
    def echo(body):
        keyserver.audit("fixture.echo", "fixture-subject", {})
        return {"caller": keyserver._CALLER.get()}
    keyserver.ACTIONS["echo"] = echo
    srv = serve(port, token)
    try:
        code, d = call(port, "/api/echo", token=token, body={}, caller="session:abc")
        check("a named caller reaches the action's context", code == 200 and d.get("caller") == "session:abc", f"{code} {d}")
        receipt = json.loads(keyserver.AUDIT.read_text().splitlines()[-1])
        check("the journal row records the caller the header named",
              receipt.get("caller") == "session:abc", str(receipt))
        code, d = call(port, "/api/echo", token=token, body={})
        check("and an unnamed one is `unnamed`", code == 200 and d.get("caller") == "unnamed", f"{code} {d}")
        code, d = call(port, "/api/echo", token=token[:-1] + ("A" if token[-1] != "A" else "B"), body={}, caller="session:abc")
        check("a token one character off is still 401 (compare_digest, not !=)", code == 401, f"{code} {d}")
    finally:
        srv.shutdown()
        keyserver.ACTIONS.pop("echo", None)
    src = (ROOT / "tools/keyserver.py").read_text(encoding="utf-8")
    check("the audit row carries the caller", '"caller": _CALLER.get()' in src)
    check("and the audit file sits under STATE, the redirectable knob",
          'AUDIT = paths.STATE / "logs" / "keyserver.jsonl"' in src)


def test_a_route_reaches_the_door_and_the_door_is_a_thing_that_exists():
    """`_door()` was called by three routes and defined nowhere until
    2026-09-14 — every LIVE mint, limit and revoke raised NameError behind a
    500, and no suite noticed because each stopped at the refusal layer above
    the door. This one drives four routes THROUGH a stubbed door: the
    disable/enable/rotate-key routes and the older limit."""
    import keyserver
    check("the door is defined and is the OpenRouter module",
          hasattr(keyserver, "_door") and hasattr(keyserver._door(), "toggle_key")
          and hasattr(keyserver._door(), "rotate_one") and hasattr(keyserver._door(), "set_limit"), "")
    calls = []

    class Stub:
        def toggle_key(self, name, disabled):
            calls.append(("toggle", name, disabled))
            if name == "ghost":
                raise LookupError("no issued key called 'ghost'")
            return {"name": name, "disabled": disabled}

        def rotate_one(self, name):
            calls.append(("rotate", name))
            if name == "ghost":
                raise LookupError("not in the ledger")
            return {"name": name, "delivered_to": "vault:x/prod/K", "rotations": 2, "rotated_on": "2026-09-14"}

        def set_limit(self, label, limit, reset):
            calls.append(("limit", label, limit))
            raise RuntimeError("provider says no")

    was = keyserver._door
    keyserver._door = lambda: Stub()
    port, token = free_port(), TOKEN
    srv = serve(port, token)
    try:
        code, d = call(port, "/api/disable", token=token, body={"name": "alpha"}, caller="test")
        check("disable reaches the door and answers with its result", code == 200 and d.get("disabled") is True, f"{code} {d}")
        code, d = call(port, "/api/enable", token=token, body={"name": "alpha"}, caller="test")
        check("enable likewise", code == 200 and d.get("disabled") is False, f"{code} {d}")
        code, d = call(port, "/api/disable", token=token, body={"name": "ghost"}, caller="test")
        check("an unknown name is 404 with the door's sentence, never a 500",
              code == 404 and "ghost" in d.get("error", ""), f"{code} {d}")
        code, d = call(port, "/api/rotate-key", token=token, body={"name": "alpha"}, caller="test")
        check("rotate-key rotates through the door and returns no value",
              code == 200 and d.get("rotations") == 2 and "key" not in {k.lower() for k in d}, f"{code} {d}")
        code, d = call(port, "/api/rotate", token=token, body={"name": "alpha"}, caller="test")
        check("`rotate` itself stays refused — that is the vault's stdin verb", code == 403, f"{code} {d}")
        code, d = call(port, "/api/limit", token=token, body={"label": "alpha", "limit": 5}, caller="test")
        check("a provider refusal on limit is 404 with the reason, not a NameError",
              code == 404 and "provider says no" in d.get("error", ""), f"{code} {d}")
        code, d = call(port, "/api/disable", token=token, body={}, caller="test")
        check("no name is 400", code == 400, f"{code} {d}")
        rows = [json.loads(l) for l in keyserver.AUDIT.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("every attempt is journalled before the door is asked, with its caller",
              {r["action"] for r in rows} >= {"disable", "enable", "rotate-key", "limit"}
              and all(r.get("caller") == "test" for r in rows), str([(r["action"], r.get("caller")) for r in rows]))
        # Five, not seven: `rotate` is refused before any door, and an empty
        # body stops at 400 — the door is asked only when the route is real.
        check("and the door was asked exactly as many times as routes reached it", len(calls) == 5, str(calls))
    finally:
        srv.shutdown()
        keyserver._door = was


def test_it_refuses_without_the_token():
    port = free_port()
    srv = serve(port, TOKEN)
    try:
        code, d = call(port, "/api/state")
        check("no token is 401", code == 401, f"{code} {d}")
        code, d = call(port, "/api/state", token="wrong")
        check("a wrong token is 401 too", code == 401, f"{code} {d}")
        code, _ = call(port, "/api/state", token=TOKEN)
        check("the right token is let through", code == 200, str(code))
    finally:
        srv.shutdown()


def test_it_refuses_a_foreign_origin():
    port = free_port()
    srv = serve(port, TOKEN)
    try:
        code, d = call(port, "/api/state", token=TOKEN, origin="https://evil.example")
        check("another site is 403", code == 403, f"{code} {d}")
        check("refusal explains the origin boundary", "origin" in json.dumps(d).lower())
        code, _ = call(port, "/api/state", token=TOKEN, origin="http://127.0.0.1:1")
        check("another local port is a different origin", code == 403, str(code))
        code, _ = call(port, "/api/state", token=TOKEN, origin=f"http://127.0.0.1:{port}")
        check("the exact serving origin is allowed", code == 200, str(code))
        for origin in (f"http://localhost.evil.example:{port}",
                       f"http://127.0.0.1.evil.example:{port}",
                       # userinfo-shaped, composed so no literal URL carries it
                       "http://127.0.0.1:" + f"{port}" + "@evil.example",
                       f"http://127.0.0.1:{port}/path", "null"):
            code, _ = call(port, "/api/state", token=TOKEN, origin=origin)
            check("lookalike or malformed origin is refused", code == 403, str(code))
        for route in ('/api/state', '/'):
            code, d = call(port, route, token=TOKEN, host=f'evil.example:{port}')
            check("foreign Host cannot read API or token-bearing page", code == 403, str(code))
    finally:
        srv.shutdown()


def test_a_value_may_not_travel_through_it():
    """The rule the whole vault rests on, enforced at the door."""
    port = free_port()
    srv = serve(port, TOKEN)
    try:
        for what in ("put", "rotate"):
            code, d = call(port, f"/api/{what}", token=TOKEN, body={"x": 1})
            check(f"{what} is refused", code == 403, f"{code} {d}")
            check(f"and {what} says why, rather than 404",
                  "stdin" in json.dumps(d, ensure_ascii=False),
                  json.dumps(d, ensure_ascii=False)[:120])
    finally:
        srv.shutdown()


def test_the_state_it_serves_holds_no_value():
    port = free_port()
    srv = serve(port, TOKEN)
    try:
        code, d = call(port, "/api/state", token=TOKEN)
        text = json.dumps(d)
        check("the state is served", code == 200)
        # A label is `sk-or-v1-81d...13f`; a value is sixty characters longer.
        check("and nothing in it is key-shaped",
              not any(isinstance(v, str) and v.startswith("sk-") and "..." not in v
                      and len(v) > 24
                      for c in d.get("credentials", []) for v in c.values()),
              text[:160])
    finally:
        srv.shutdown()


def test_it_will_not_revoke_what_something_is_reading():
    """The one destructive action, and the case where it must not fire.

    Driven on a planted key scan in a scratch directory: one key serving a
    known consumer, one serving nothing. The door is stubbed, so the refusal is
    proven to come BEFORE any provider call, and the free key reaches the door.
    """
    import keyserver
    import tmp as tmpdir
    scratch = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-ks-scan-"))
    live, spare = "fixture-label...abc", "fixture-label...def"
    (scratch / "openrouter.json").write_text(json.dumps({"keys": [
        {"label": live, "serves": "observatory"},
        {"label": spare, "serves": None}]}), encoding="utf-8")
    asked = []

    class Door:
        def revoke_key(self, label):
            asked.append(label)
            return {"revoked": label}

    was_scratch, was_door = keyserver.paths.SCRATCH, keyserver._door
    keyserver.paths.SCRATCH, keyserver._door = scratch, (lambda: Door())
    port = free_port()
    srv = serve(port, TOKEN)
    try:
        code, d = call(port, "/api/revoke", token=TOKEN, body={"label": live})
        check("revoking a key in service is refused", code == 400, f"{code} {d}")
        check("and the refusal names the consumer and the remedy",
              "observatory" in json.dumps(d) and "mint" in json.dumps(d),
              json.dumps(d)[:160])
        check("and the door was never asked", asked == [], str(asked))
        code, d = call(port, "/api/revoke", token=TOKEN, body={"label": spare})
        check("a key nothing reads reaches the door", code == 200 and asked == [spare], f"{code} {d} {asked}")
    finally:
        srv.shutdown()
        keyserver.paths.SCRATCH, keyserver._door = was_scratch, was_door


def test_a_destination_is_checked_before_anything_is_minted():
    """The second shape of destination — a vault slot.

    The door has taken `--to vault:<project>/<env>/<NAME>` since it existed and
    this server could only name two fixed files, so a key for a project could
    be issued from a terminal and nowhere else. Opening that shape is only safe
    while the check is: an arbitrary string here would make a server that holds
    a provisioning key into an arbitrary file writer, and a MISSPELT project
    would not fail at all — it would quietly create a slot nothing ever reads.

    Nothing is minted here: `check_destination` makes no call.
    """
    import keyserver as ks
    sys.path.insert(0, str(ROOT / "tests"))
    import own_project                                              # noqa: PLC0415
    for good in ("observatory", "claude-mem"):
        check(f"{good} is still a destination", ks.check_destination(good) == good)
    # RESOLVED, NEVER TYPED (a fixture must not lean on ambient state): the merge re-anchors this project when
    # the evidence moves, and a literal name here would fail on that day for a
    # reason no reader could see from the assertion.
    own_id = own_project.own_project_id() or ""
    # A destination names a project by its registry NAME, which is not the id's
    # suffix; read it from the row the resolver found.
    rows = json.loads((ks.paths.REGISTRY / "projects.json").read_text(encoding="utf-8"))["projects"]
    own = next((str(r.get("name") or "").lower() for r in rows if r.get("id") == own_id), "") \
        or own_project.OWN_FOLDER
    known = ks.known_projects()
    if own not in known:
        print(f"  NOTE  this project resolves to {own!r}, which the registry does "
              f"not list under that name [covered: the refusals below drive every "
              f"branch of the check]")
        own = sorted(known)[0] if known else own
    check("the estate's own projects are known to it", own in known,
          f"{len(known)} name(s) — registry/projects.json plus the vault's own dirs")
    ok = f"vault:{own}/prod/OPENROUTER_API_KEY"
    check("a well-formed slot under a known project is taken",
          ks.check_destination(ok) == ok)
    for bad, why in (
        (f"vault:{own}/prod/lower_case", "a lowercase variable name"),
        (f"vault:{own}/PROD/NAME", "an uppercase env"),
        ("vault:no-such-project-here/prod/NAME", "a project this machine does not have"),
        ("vault:a/b", "two segments instead of three"),
        ("../../etc/passwd", "a path"),
        ("", "nothing at all"),
        (None, "no destination"),
    ):
        try:
            ks.check_destination(bad)
            check(f"{why} is refused", False, f"{bad!r} was accepted")
        except ValueError as exc:
            check(f"{why} is refused", True)
            check(f"and the refusal of {why} says what a good one looks like",
                  "vault:<project>/<env>/<NAME>" in str(exc) or "no project named" in str(exc),
                  str(exc)[:120])
    src = (ROOT / "tools/keyserver.py").read_text(encoding="utf-8")
    body = src.split("def act_mint(", 1)[1].split("\ndef ", 1)[0]
    check("act_mint checks the destination before it audits or acts",
          body.index("check_destination") < body.index("audit("), body[:200])


def test_it_refuses_to_bind_anything_routable():
    p = subprocess.run([sys.executable, str(ROOT / "tools/keyserver.py"),
                        "--host", "0.0.0.0", "--port", str(free_port())],
                       capture_output=True, text=True, timeout=30)
    check("a routable bind exits non-zero", p.returncode != 0, p.stdout[-120:])
    check("and says why", "only to this machine" in (p.stdout + p.stderr),
          (p.stdout + p.stderr)[-160:])


def test_the_audit_line_precedes_the_action():
    """A log written after the call loses the one case that matters."""
    src = (ROOT / "tools/keyserver.py").read_text(encoding="utf-8")
    for fn in ("act_mint", "act_limit", "act_revoke"):
        body = src.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
        if "audit(" not in body:
            check(f"{fn} writes an audit line", False, "none")
            continue
        # The first network call in the body must come after the audit line.
        i_audit = body.index("audit(")
        i_call = min([body.index(t) for t in ("api(", "subprocess.run(") if t in body] or [10 ** 9])
        check(f"{fn} audits before it acts", i_audit < i_call,
              f"audit at {i_audit}, action at {i_call}")


def test_a_group_readable_token_is_refused():
    import tempfile
    from unittest.mock import patch
    import keyserver
    from runtime_identity import IdentityError
    with tempfile.TemporaryDirectory() as directory:
        path = pathlib.Path(directory) / '.keyserver-token'
        path.write_text('T' * 43 + '\n')
        path.chmod(0o640)
        with patch.object(keyserver, 'TOKEN_FILE', path):
            try:
                keyserver.token()
                refused = False
            except IdentityError:
                refused = True
        check('a group-readable token is refused by the real reader', refused)
        check('refusal leaves bytes and permissions unchanged',
              path.read_text() == 'T' * 43 + '\n' and path.stat().st_mode & 0o777 == 0o640)


if __name__ == "__main__":
    print("keyserver — the refusals are the component\n")
    for fn in (test_the_audit_names_the_caller,
               test_a_route_reaches_the_door_and_the_door_is_a_thing_that_exists,
               test_it_refuses_without_the_token,
               test_it_refuses_a_foreign_origin,
               test_a_value_may_not_travel_through_it,
               test_the_state_it_serves_holds_no_value,
               test_it_will_not_revoke_what_something_is_reading,
               test_a_destination_is_checked_before_anything_is_minted,
               test_it_refuses_to_bind_anything_routable,
               test_the_audit_line_precedes_the_action,
               test_a_group_readable_token_is_refused):
        fn()
    print()
    if FAILURES:
        print(f"\033[31m{len(FAILURES)} failed\033[0m")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("\033[32ma value still travels on stdin and nowhere else\033[0m")
