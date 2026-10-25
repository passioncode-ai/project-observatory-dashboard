#!/usr/bin/env python3
"""The Heroku door (`tools/heroku.py`, OBS-156) against a fake Platform API.

Nothing here reaches Heroku: the provider is `FakeHeroku`, a dict of
authorizations, teams, members and apps answering the endpoints the door calls,
and the stores are temporary folders. What is asserted is the door's reasoning:
- a value never reaches argv, stdout, stderr, the ledger or a meta file;
- an authorization is proved with its OWN value before the vault gets it, and one
  this call created is deleted again when the proof or the delivery fails;
- a second issue regenerates in place rather than leaving a twin, and a changed
  preset deletes the slot's old authorization only after the new one is delivered;
- membership writes are a plan until `--apply`, are verified by reading the team
  again, and refuse `owner`, the last admin and the admin's own membership.
"""
from __future__ import annotations
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import types
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402  sandboxes HOME first
import private_io                                                   # noqa: E402

FAILURES: list[str] = []
#: The release proof's pinned Range: the newest release only (Platform API ranges).
RELEASE_RANGE = "version ..; order=desc, max=1"
SLUG_ID = "12345678-1234-1234-1234-123456789abc"
OLD_SLUG_ID = "87654321-4321-4321-4321-cba987654321"
ADMIN = "admin-session-" + "a" * 30
SEEN_VALUES: list[str] = [ADMIN]


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f": {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def door():
    spec = importlib.util.spec_from_file_location("heroku_door", ROOT / "tools" / "heroku.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    base = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = base / "heroku-admin"
    m.LEDGER = base / "heroku-issued.json"
    m._journal = lambda *a, **k: None
    return m


class FakeHeroku:
    """The endpoints the door calls, with Heroku's shapes and statuses."""

    def __init__(self, m):
        self.m = m
        self.calls: list[tuple[str, str, str, dict | None]] = []
        self.accepts: list[tuple[str, str, str | None]] = []
        self.auths: dict[str, dict] = {}
        self.n = 0
        # example-team takes people by invitation (the feature on, as on every real
        # team the door was measured against); direct-team adds them directly.
        self.teams = {"example-team": {"id": "team-1", "name": "example-team", "role": "admin", "type": "team"},
                      "other-team": {"id": "team-2", "name": "other-team", "role": "member", "type": "team"},
                      "direct-team": {"id": "team-3", "name": "direct-team", "role": "admin", "type": "team"}}
        self.features = {"example-team": [{"name": "team-invite-acceptance", "enabled": True},
                                          {"name": "something-else", "enabled": True}],
                         "other-team": [], "direct-team": [{"name": "team-invite-acceptance", "enabled": False}]}
        self.invitations = {"example-team": [
            {"id": "inv-0001", "user": {"email": "pending@example.invalid"}, "role": "member"}]}
        roster = [{"email": "admin@example.invalid", "role": "admin", "two_factor_authentication": True},
                  {"email": "dev@example.invalid", "role": "member", "two_factor_authentication": False},
                  {"email": "boss@example.invalid", "role": "owner", "two_factor_authentication": True}]
        self.members = {"example-team": [dict(r) for r in roster], "direct-team": [dict(r) for r in roster]}
        self.apps = {"example-team": [{"name": "alpha-web", "region": {"name": "eu"}, "locked": False,
                                       "updated_at": "2026-10-01T00:00:00Z"}],
                     "other-team": []}
        self.refuse: dict[str, int] = {}        # path prefix -> status, for the next matching call
        self.can_list_auths = True
        self.ignore_member_writes = False
        self.slow = 0.0                         # seconds a mint takes, to force interleaving
        self.ranges: list[tuple[str, str | None]] = []
        self.lock = __import__("threading").Lock()

    def mint_value(self) -> str:
        self.n += 1
        v = f"HRKU-synthetic-{self.n:02d}-" + "z" * 40
        SEEN_VALUES.append(v)
        return v

    def by_token(self, token: str) -> dict | None:
        return next((a for a in self.auths.values() if a["token"] == token), None)

    def __call__(self, method, path, token, payload=None, rng=None, accept=None):
        with self.lock:
            self.calls.append((method, path, token, payload))
            self.accepts.append((method, path, accept))
        for prefix, status in list(self.refuse.items()):
            if path.startswith(prefix):
                raise self.m.HerokuError(status, f"heroku answered HTTP {status} on {self.m._shape(path)}; "
                                                 "provider response withheld")
        admin = token == ADMIN
        held = self.by_token(token)
        if not admin and not held:
            raise self.m.HerokuError(401, "heroku answered HTTP 401")
        if path == "/account":
            return {"id": "user-1", "email": "admin@example.invalid"}, None
        if path == "/oauth/authorizations" and method == "GET":
            if not admin or not self.can_list_auths:
                raise self.m.HerokuError(403, "heroku answered HTTP 403")
            return [{k: v for k, v in a.items() if k != "token"} for a in self.auths.values()], None
        if path == "/oauth/authorizations" and method == "POST":
            if self.slow:
                __import__("time").sleep(self.slow)
            with self.lock:
                self.n_created = getattr(self, "n_created", 0) + 1
                aid = f"auth-{self.n_created:04d}-0000"
                self.auths[aid] = {"id": aid, "description": payload.get("description"),
                                   "scope": list(payload["scope"]), "token": self.mint_value(),
                                   "expires_in": payload.get("expires_in")}
                return self._full(aid), None
        if path.startswith("/oauth/authorizations/"):
            aid = path.split("/")[3]
            if aid not in self.auths:
                raise self.m.HerokuError(404, "heroku answered HTTP 404")
            if path.endswith("/actions/regenerate-tokens"):
                if self.slow:
                    __import__("time").sleep(self.slow)
                with self.lock:
                    self.auths[aid]["token"] = self.mint_value()
                    return self._full(aid), None
            if method == "DELETE":
                return {"id": self.auths.pop(aid)["id"]}, None
        if path == "/teams":
            return list(self.teams.values()), None
        if path.startswith("/teams/"):
            parts = path.split("/")
            team = next((t for t in self.teams.values() if parts[2] in (t["id"], t["name"])), None)
            if not team:
                raise self.m.HerokuError(404, "heroku answered HTTP 404")
            if held and "read" not in held["scope"] and "read-protected" not in held["scope"]:
                raise self.m.HerokuError(403, "heroku answered HTTP 403")
            if len(parts) == 3:
                return team, None
            if parts[3] == "apps":
                return self.apps.get(team["name"], []), None
            if parts[3] == "features":
                return self.features.get(team["name"], []), None
            if parts[3] == "invitations":
                if accept != self.m.ACCEPT_INVITATIONS:
                    raise self.m.HerokuError(406, "heroku answered HTTP 406")
                rows = self.invitations.setdefault(team["name"], [])
                if method == "GET":
                    return list(rows), None
                if self.ignore_member_writes:
                    return {}, None
                if method == "PUT":
                    rows.append({"id": f"inv-{len(rows) + 9:04d}", "user": {"email": payload["email"]},
                                 "role": payload["role"]})
                    return {}, None
                if method == "DELETE":
                    gone = parts[4].replace("%40", "@")
                    self.invitations[team["name"]] = [r for r in rows if gone not in (r["id"], r["user"]["email"])]
                    return {}, None
            if parts[3] == "members":
                rows = self.members.setdefault(team["name"], [])
                if method == "GET":
                    # two pages, so a reader that stops at page one is caught
                    if rng is None and len(rows) > 1:
                        return rows[:1], "email ]x..; max=1"
                    return (rows[1:] if rng else rows), None
                if self.ignore_member_writes:
                    return {}, None
                if method in ("PUT", "PATCH"):
                    hit = next((r for r in rows if r["email"] == payload["email"]), None)
                    if hit:
                        hit["role"] = payload["role"]
                    else:
                        rows.append({"email": payload["email"], "role": payload["role"]})
                    return {}, None
                if method == "DELETE":
                    gone = parts[4].replace("%40", "@")
                    self.members[team["name"]] = [r for r in rows if r["email"] != gone]
                    return {}, None
        if path.startswith("/apps"):
            if held and not set(held["scope"]) & {"read", "read-protected"}:
                raise self.m.HerokuError(403, "heroku answered HTTP 403")
            if path == "/apps":
                return [a for v in self.apps.values() for a in v], None
            name = path.split("/")[2]
            hit = next((a for v in self.apps.values() for a in v if a["name"] == name), None)
            if not hit:
                raise self.m.HerokuError(404, "heroku answered HTTP 404")
            if len(path.split("/")) >= 4 and path.split("/")[3] in ("releases", "builds", "slugs"):
                if held and "read-protected" not in held["scope"]:
                    raise self.m.HerokuError(403, "heroku answered HTTP 403")
                if path.endswith("/releases"):
                    # Heroku lists releases oldest first unless the Range asks for the
                    # newest one; a reader that drops the pinned Range gets two rows.
                    with self.lock:
                        self.ranges.append((path, rng))
                    newest = {"version": 7, "slug": {"id": SLUG_ID}}
                    if rng == RELEASE_RANGE:
                        return [newest], None
                    return [{"version": 6, "slug": {"id": OLD_SLUG_ID}}, newest], None
                if path.endswith("/slugs"):
                    raise self.m.HerokuError(404, "heroku answered HTTP 404")
                if path.split("/")[3] == "slugs":
                    if path.split("/")[4] not in (SLUG_ID, OLD_SLUG_ID):
                        raise self.m.HerokuError(404, "heroku answered HTTP 404")
                    return {"id": path.split("/")[4], "process_types": {"web": "synthetic"}}, None
                return [], None
            return hit, None
        raise AssertionError(f"unexpected call {method} {path}")

    def _full(self, aid):
        a = self.auths[aid]
        return {"id": aid, "description": a["description"], "scope": a["scope"],
                "access_token": {"token": a["token"], "expires_in": a["expires_in"]},
                "refresh_token": {"token": "refresh-" + a["token"]}}


def with_admin(m, email="admin@example.invalid"):
    m.ADMIN_STORE.mkdir(parents=True, exist_ok=True)
    private_io.write(m.ADMIN_STORE / "admin-example-invalid", ADMIN + "\n")
    private_io.write(m.ADMIN_STORE / "admin-example-invalid.meta.json",
                     json.dumps({"kind": "admin", "email": email}))


def setup(vault_ok=True):
    m = door()
    fake = FakeHeroku(m)
    m._call = fake
    delivered: list[tuple] = []

    def deliver(value, project, env, name):
        if not vault_ok:
            raise RuntimeError(f"vault refused the slot {project}/{env}/{name} (exit 3); child output withheld")
        delivered.append((value, project, env, name))
    m.deliver_to_vault = deliver
    m.parse_vault_target = lambda t: tuple(t.split("/"))
    with_admin(m)
    return m, fake, delivered


@contextlib.contextmanager
def patched(obj, name, value):
    """Module attributes such as `subprocess.run` are shared by every suite in this
    process, so each patch is undone when its block ends."""
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def run(fn, *a, **k):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*a, **k)
    return rc, out.getvalue(), err.getvalue()


def no_value(text: str) -> bool:
    return not any(v in text for v in SEEN_VALUES) and "HRKU-" not in text and "refresh-" not in text


# ─────────────────────────── stash ──────────────────────────────────────────

def test_stash_keeps_the_admin_private_and_refuses_what_cannot_issue() -> None:
    m = door()
    fake = FakeHeroku(m)
    m._call = fake
    rc, _o, e = run(m.cmd_stash, "", None)
    check("an empty stdin is refused", rc == 2, str(rc))
    rc, _o, e = run(m.cmd_stash, "two pieces", None)
    check("two pieces are refused", rc == 1, e)
    fake.can_list_auths = False
    rc, _o, e = run(m.cmd_stash, ADMIN, None)
    check("a token that cannot list authorizations is refused and nothing is written",
          rc == 1 and not m.ADMIN_STORE.exists(), e)
    fake.can_list_auths = True
    rc, o, e = run(m.cmd_stash, ADMIN, None)
    stash = m.ADMIN_STORE / "admin-example-invalid"
    check("a global token is stashed under the account's email as a slug", rc == 0 and stash.is_file(), o + e)
    check("at mode 600", stash.stat().st_mode & 0o777 == 0o600, oct(stash.stat().st_mode))
    meta = json.loads((m.ADMIN_STORE / "admin-example-invalid.meta.json").read_text())
    check("its record names the account and the teams it sees, and holds no value",
          meta.get("account_id") == "user-1" and meta.get("teams") == 3 and ADMIN not in json.dumps(meta), str(meta))
    check("and nothing printed the value", no_value(o + e), o + e)


def test_stash_from_cli_reads_the_session_token_without_argv() -> None:
    m = door()
    m._call = FakeHeroku(m)
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, "warning line\n" + ADMIN + "\n", "CLI-STDERR"

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return Done()
    with patched(m.subprocess, "run", fake_run), patched(m.shutil, "which", lambda name: "/usr/bin/heroku"):
        rc, o, e = run(lambda: m.cmd_stash(m.cli_token(), None))
    check("the CLI is asked for its token with no value in argv",
          seen["argv"] == ["heroku", "auth:token"] and seen["kw"].get("capture_output"), str(seen))
    check("and with stdin closed, so a CLI prompt cannot wait on the terminal",
          seen["kw"].get("stdin") is m.subprocess.DEVNULL, str(seen["kw"]))
    check("its last stdout line is what is stashed", rc == 0
          and (m.ADMIN_STORE / "admin-example-invalid").read_text().strip() == ADMIN, o + e)
    check("and neither the value nor the CLI's stderr is printed", no_value(o + e) and "CLI-STDERR" not in o + e, o + e)


# ─────────────────────────── issue ──────────────────────────────────────────

def test_issue_mints_a_narrow_authorization_proves_it_and_delivers_it() -> None:
    m, fake, delivered = setup()
    rc, o, e = run(m.cmd_issue, "read", "alpha/prod/HEROKU_API_KEY", "example-team", None, None, None, wait=0)
    check("read issues", rc == 0, o + e)
    created = [p for meth, path, tok, p in fake.calls if meth == "POST" and path == "/oauth/authorizations"]
    check("one authorization, scope read only, never global",
          len(created) == 1 and created[0]["scope"] == ["read"], str(created))
    check("its description names the preset, the slot and the team",
          created[0]["description"] == "observatory read alpha/prod/HEROKU_API_KEY team example-team (managed)",
          str(created))
    new = next(iter(fake.auths.values()))["token"]
    check("proved with the NEW value on the team's apps before delivery",
          any(tok == new and path == "/teams/example-team/apps" for _m, path, tok, _p in fake.calls), "")
    check("delivered to the slot", delivered == [(new, "alpha", "prod", "HEROKU_API_KEY")], str(delivered))
    row = json.loads(m.LEDGER.read_text())["issued"]["alpha/prod/HEROKU_API_KEY"]
    check("the ledger row has the id, scope and team, and no value",
          row["authorization_id"] == "auth-0001-0000" and row["scopes"] == ["read"]
          and row["team"] == "example-team" and no_value(m.LEDGER.read_text()), str(row))
    check("the ledger is owner-only", m.LEDGER.stat().st_mode & 0o777 == 0o600, "")
    check("nothing printed a value or the refresh token", no_value(o + e), o + e)
    check("the output says Heroku cannot narrow to a team", "cannot narrow" in o, o)
    check("no preset issues global", all("global" not in p["scopes"] for p in m.PRESETS.values()), "")


def test_issue_refuses_before_minting() -> None:
    m, fake, delivered = setup()
    rc, _o, e = run(m.cmd_issue, "read", None, None, None, None, None, wait=0)
    check("no --vault is refused", rc == 2, e)
    rc, _o, e = run(m.cmd_issue, "config-read", "alpha/prod/K", None, None, None, None, wait=0)
    check("a config preset without --app is refused", rc == 2 and "--app" in e, e)
    rc, _o, e = run(m.cmd_issue, "read", "alpha/prod/K", "missing-team", None, None, None, wait=0)
    check("an unknown team is refused", rc == 1, e)
    rc, _o, e = run(m.cmd_issue, "read", "alpha/prod/K", None, None, None, 0, wait=0)
    check("an expiry of 0 days is refused", rc == 2, e)
    check("and none of them minted anything", not fake.auths and not delivered, str(fake.auths))


def test_issue_deletes_what_it_created_when_proof_or_delivery_fails() -> None:
    m, fake, delivered = setup()
    rc, o, e = run(m.cmd_issue, "identity", "alpha/prod/K", "example-team", None, None, None, wait=0)
    check("an identity authorization cannot prove a team read... it is not asked to",
          rc == 0, o + e)
    m, fake, delivered = setup()
    fake.refuse["/apps/alpha-web"] = 403
    rc, o, e = run(m.cmd_issue, "deploy", "alpha/prod/K", None, "alpha-web", None, None, wait=0)
    check("an app the admin cannot read is refused before minting", rc == 1 and not fake.auths, e)
    m, fake, delivered = setup(vault_ok=False)
    rc, o, e = run(m.cmd_issue, "read", "alpha/prod/K", None, None, None, None, wait=0)
    check("a vault refusal fails the issue", rc == 1, e)
    check("and the authorization it created is deleted at Heroku", not fake.auths and "deleted again" in e, e)
    check("and no ledger row is written", not m.LEDGER.exists(), "")
    check("and the refusal prints no value", no_value(o + e), o + e)


def test_issue_twice_regenerates_in_place_and_a_new_preset_retires_the_old() -> None:
    m, fake, delivered = setup()
    run(m.cmd_issue, "read", "alpha/prod/K", None, "alpha-web", None, None, wait=0)
    first = dict(fake.auths)
    rc, o, e = run(m.cmd_issue, "read", "alpha/prod/K", None, "alpha-web", None, None, wait=0)
    check("a second issue regenerates the same authorization",
          rc == 0 and list(fake.auths) == list(first) and len(delivered) == 2
          and delivered[0][0] != delivered[1][0], o + e)
    row = json.loads(m.LEDGER.read_text())["issued"]["alpha/prod/K"]
    check("and counts a rotation", row["rotations"] == 1 and row["rotated_on"], str(row))
    rc, o, e = run(m.cmd_issue, "deploy", "alpha/prod/K", None, "alpha-web", None, 30, wait=0)
    check("a changed preset creates a new authorization", rc == 0 and len(fake.auths) == 1
          and next(iter(fake.auths.values()))["scope"] == ["read", "write"], str(fake.auths))
    check("with the asked expiry in seconds", next(iter(fake.auths.values()))["expires_in"] == 30 * 86400, "")
    check("and the old one is deleted only after delivery", "previous authorization auth-0001-0000 was deleted" in o
          and len(delivered) == 3, o)


# ─────────────────────────── rotate, revoke, ping, list ─────────────────────

def test_rotate_regenerates_proves_and_delivers() -> None:
    m, fake, delivered = setup()
    run(m.cmd_issue, "read", "alpha/prod/K", "example-team", None, None, None, wait=0)
    rc, o, e = run(m.cmd_rotate, "alpha/prod/K", False, wait=0)
    check("rotate regenerates in place", rc == 0 and len(fake.auths) == 1 and len(delivered) == 2
          and delivered[1][0] == next(iter(fake.auths.values()))["token"], o + e)
    check("by the regenerate action", any(p.endswith("/actions/regenerate-tokens") for _m, p, _t, _x in fake.calls), "")
    rc, o, e = run(m.cmd_rotate, "auth-0001-0000", False, wait=0)
    check("an authorization id names the same row", rc == 0, o + e)
    m.deliver_to_vault = lambda *a: (_ for _ in ()).throw(RuntimeError("vault refused the slot (exit 3)"))
    rc, o, e = run(m.cmd_rotate, "alpha/prod/K", False, wait=0)
    check("a failed delivery after regenerate says the slot's value is dead",
          rc == 1 and "REGENERATED but not delivered" in e and no_value(o + e), e)


def test_rotate_leaked_reads_the_vault_register() -> None:
    m, fake, delivered = setup()
    run(m.cmd_issue, "read", "alpha/prod/K", None, None, None, None, wait=0)
    run(m.cmd_issue, "read", "beta/prod/K", None, None, None, None, wait=0)
    fake_vault = types.SimpleNamespace(open_leaks=lambda: [{"event": "leaked", "secret": "beta/prod/K"}])
    sys.modules["vault"] = fake_vault
    try:
        rc, o, e = run(m.cmd_rotate, None, True, wait=0)
    finally:
        sys.modules.pop("vault", None)
    rows = json.loads(m.LEDGER.read_text())["issued"]
    check("only the leaked slot is rotated", rc == 0 and rows["beta/prod/K"]["rotations"] == 1
          and rows["alpha/prod/K"]["rotations"] == 0, o + e)
    check("and settle is named to close the leak", "settle" in o, o)


def test_revoke_deletes_at_heroku_before_the_ledger() -> None:
    m, fake, delivered = setup()
    run(m.cmd_issue, "read", "alpha/prod/K", None, None, None, None, wait=0)
    fake.refuse["/oauth/authorizations/auth-0001-0000"] = 500
    rc, o, e = run(m.cmd_revoke, "alpha/prod/K")
    check("a provider failure keeps the ledger row", rc == 1
          and "alpha/prod/K" in json.loads(m.LEDGER.read_text())["issued"], e)
    fake.refuse.clear()
    rc, o, e = run(m.cmd_revoke, "alpha/prod/K")
    check("revoke deletes at Heroku and then the row", rc == 0 and not fake.auths
          and not json.loads(m.LEDGER.read_text())["issued"], o + e)
    check("and names the slot that now holds a dead value", "dead value" in o, o)


def test_ping_and_list_report_without_values() -> None:
    m, fake, delivered = setup()
    run(m.cmd_issue, "read", "alpha/prod/K", None, None, None, None, wait=0)
    run(m.cmd_issue, "deploy", "beta/prod/K", None, None, None, None, wait=0)
    fake.auths["stray-0001-0000"] = {"id": "stray-0001-0000", "description": "observatory read x/y/z (managed)",
                                     "scope": ["read"], "token": "HRKU-stray", "expires_in": None}
    rc, o, e = run(m.cmd_ping)
    check("ping passes when every row is alive", rc == 0, o + e)
    check("and names a managed authorization no row holds", "stray-0001-0000" in o, o)
    fake.auths.pop("auth-0001-0000")
    fake.auths["auth-0002-0000"]["scope"] = ["global"]
    rc, o, e = run(m.cmd_ping)
    check("ping fails on a gone authorization and a widened scope",
          rc == 1 and "GONE" in e and "scope at Heroku" in e, e)
    rc, o, e = run(m.cmd_list)
    check("list names slots, presets, scopes and ids", rc == 0 and "beta/prod/K" in o and "read,write" in o, o)
    check("and no value", no_value(o + e), o + e)


# ─────────────────────────── teams ──────────────────────────────────────────

def test_teams_members_and_apps_read_with_their_slots() -> None:
    m, fake, delivered = setup()
    run(m.cmd_issue, "read", "alpha/prod/K", "example-team", "alpha-web", None, None, wait=0)
    rc, o, e = run(m.cmd_teams, None)
    check("teams lists every team with role and app count", rc == 0 and "example-team" in o
          and "role admin" in o and "apps 1" in o, o + e)
    check("and the slot issued for it", "slots alpha/prod/K" in o, o)
    rc, o, e = run(m.cmd_members, "example-team", None)
    check("members reads every page", rc == 0 and "members of example-team (3, and 1 pending" in o
          and "dev@example.invalid" in o and "2fa off" in o, o + e)
    check("and lists the pending invitation, marked pending",
          any("pending@example.invalid" in ln and "pending invitation" in ln for ln in o.splitlines()), o)
    check("read with the invitations Accept variant",
          ("GET", "/teams/example-team/invitations", m.ACCEPT_INVITATIONS) in fake.accepts, "")
    rc, o, e = run(m.cmd_members, "direct-team", None)
    check("a team without the feature reads no invitations", rc == 0 and "pending" not in o
          and not any(p.endswith("/invitations") for _m, p, _a in fake.accepts if "direct-team" in p), o)
    rc, o, e = run(m.cmd_apps, "example-team", None)
    check("apps lists the team's apps and their slots", rc == 0 and "alpha-web" in o and "alpha/prod/K" in o, o + e)


def test_member_writes_are_a_plan_until_apply_and_are_verified() -> None:
    """A team that adds members directly (`team-invite-acceptance` off)."""
    m, fake, delivered = setup()
    rc, o, e = run(m.cmd_member, "add", "direct-team", "new@example.invalid", "member", None, False)
    writes = [c for c in fake.calls if c[0] in ("PUT", "PATCH", "DELETE") and "/members" in c[1]]
    check("a dry run prints the plan and writes nothing", rc == 0 and "add new@example.invalid as member" in o
          and "dry run" in o and not writes, o + e)
    rc, o, e = run(m.cmd_member, "add", "direct-team", "new@example.invalid", "member", None, True)
    check("--apply adds and verifies", rc == 0 and "applied and verified" in o
          and any(r["email"] == "new@example.invalid" for r in fake.members["direct-team"]), o + e)
    rc, o, e = run(m.cmd_member, "role", "direct-team", "dev@example.invalid", "admin", None, True)
    check("--apply changes a role", rc == 0 and next(r for r in fake.members["direct-team"]
                                                     if r["email"] == "dev@example.invalid")["role"] == "admin", o + e)
    rc, o, e = run(m.cmd_member, "remove", "direct-team", "new@example.invalid", None, None, True)
    check("--apply removes", rc == 0 and all(r["email"] != "new@example.invalid"
                                             for r in fake.members["direct-team"]), o + e)
    rc, o, e = run(m.cmd_member, "role", "direct-team", "dev@example.invalid", "admin", None, True)
    check("a role already held is no change and no call", rc == 0 and "no change" in o, o)
    fake.ignore_member_writes = True
    rc, o, e = run(m.cmd_member, "add", "direct-team", "ghost@example.invalid", "member", None, True)
    check("a write the team does not show afterwards fails", rc == 1 and "now reads" in e, e)


def test_a_team_that_invites_gets_invitations_the_cli_way() -> None:
    """`team-invite-acceptance` on: add is an invitation, remove of a pending invitee
    cancels it, both under the invitations Accept variant, verified on the
    invitations list; a pending invitee is not a member and manages nothing."""
    m, fake, delivered = setup()
    team = "example-team"
    rc, o, e = run(m.cmd_member, "add", team, "new@example.invalid", "member", None, False)
    writes = lambda: [c for c in fake.calls if c[0] in ("PUT", "PATCH", "DELETE")]
    check("the plan says invite, and a dry run writes nothing",
          rc == 0 and "invite new@example.invalid as member" in o and not writes(), o + e)
    rc, o, e = run(m.cmd_member, "add", team, "new@example.invalid", "member", None, True)
    check("--apply sends an invitation, not a membership",
          rc == 0 and ("PUT", "/teams/example-team/invitations", m.ACCEPT_INVITATIONS) in fake.accepts
          and not any(c[0] == "PUT" and c[1].endswith("/members") for c in fake.calls), o + e)
    check("verified on the invitations list", "applied and verified" in o
          and any(i["user"]["email"] == "new@example.invalid" for i in fake.invitations[team]), o)
    check("the members list is unchanged until they accept",
          all(r["email"] != "new@example.invalid" for r in fake.members[team]), "")
    rc, o, e = run(m.cmd_member, "add", team, "pending@example.invalid", "member", None, True)
    check("inviting someone already invited with that role is no change", rc == 0 and "no change" in o, o + e)
    rc, o, e = run(m.cmd_member, "add", team, "pending@example.invalid", "admin", None, True)
    check("and with another role is refused", rc == 1 and "pending invitation" in e, e)
    rc, o, e = run(m.cmd_member, "role", team, "pending@example.invalid", "admin", None, True)
    check("a pending invitee has no role to change", rc == 1 and "not a membership" in e, e)
    rc, o, e = run(m.cmd_member, "remove", team, "pending@example.invalid", None, None, False)
    check("remove of a pending invitee plans to cancel the invitation",
          rc == 0 and "cancel the pending invitation" in o and not writes()[2:], o + e)
    rc, o, e = run(m.cmd_member, "remove", team, "pending@example.invalid", None, None, True)
    check("--apply cancels it through the invitations endpoint, by its id",
          rc == 0 and ("DELETE", "/teams/example-team/invitations/inv-0001", m.ACCEPT_INVITATIONS) in fake.accepts
          and all(i["user"]["email"] != "pending@example.invalid" for i in fake.invitations[team]), o + e)
    rc, o, e = run(m.cmd_member, "remove", team, "dev@example.invalid", None, None, True)
    check("a real member of an inviting team is removed as a membership",
          rc == 0 and any(c[0] == "DELETE" and "/members/" in c[1] for c in fake.calls), o + e)
    fake.ignore_member_writes = True
    rc, o, e = run(m.cmd_member, "add", team, "ghost@example.invalid", "member", None, True)
    check("an invitation the invitations list does not show afterwards fails",
          rc == 1 and "pending invitations now read" in e, e)
    invited_admin = [{"email": "solo@example.invalid", "role": "admin"}]
    check("a pending invitee as admin does not count as a second admin",
          "last admin" in (m.plan_membership("remove", invited_admin, "solo@example.invalid", None, None,
                                             [{"email": "next@example.invalid", "role": "admin"}], True)[1] or ""), "")
    fake.ignore_member_writes = False
    fake.refuse["/teams/example-team/features"] = 500
    rc, o, e = run(m.cmd_member, "add", team, "x@example.invalid", "member", None, True)
    check("an unreadable feature list refuses rather than guessing the endpoint",
          rc == 1 and not any(c[1].endswith("x@example.invalid") for c in fake.calls), e)


def test_member_writes_refuse_what_would_lock_the_team() -> None:
    m, fake, delivered = setup()
    members = fake.members["example-team"]
    plan = m.plan_membership
    check("owner is refused", plan("add", members, "x@example.invalid", "owner", None)[1] is not None, "")
    check("the admin's own membership is refused",
          plan("remove", members, "admin@example.invalid", None, "admin@example.invalid")[1] is not None, "")
    no_owner = [r for r in members if r["role"] != "owner"]
    check("the last admin of a team with no owner cannot be removed",
          "last admin" in (plan("remove", no_owner, "admin@example.invalid", None, None)[1] or ""), "")
    check("nor demoted", "last admin" in (plan("role", no_owner, "admin@example.invalid", "member", None)[1] or ""), "")
    check("an owner still manages the team, so the admin beside one may go",
          plan("remove", members, "admin@example.invalid", None, None)[1] is None, "")
    check("the owner is not touched", plan("remove", members, "boss@example.invalid", None, None)[1] is not None, "")
    check("a non-member cannot be removed", plan("remove", members, "nobody@example.invalid", None, None)[1] is not None, "")
    check("adding an existing member points at `role`",
          "member role" in (plan("add", members, "dev@example.invalid", "admin", None)[1] or ""), "")
    rc, o, e = run(m.cmd_member, "remove", "example-team", "admin@example.invalid", None, None, True)
    check("and the command refuses without a write", rc == 1
          and not [c for c in fake.calls if c[0] == "DELETE"], e)


# ─────────────────────────── the wire ───────────────────────────────────────

def test_concurrent_door_runs_keep_every_ledger_row() -> None:
    """Two `issue` runs and a `rotate` at once (M1 of review round 1): without one
    lock across read-modify-write, one issue's row was lost while both
    authorizations lived, and a rotate racing an issue wrote back a stale row, so
    `ping` called a live authorization a stray. Every row must survive, name a live
    authorization, and `ping` must find nothing stray."""
    import threading
    m, fake, delivered = setup()
    run(m.cmd_issue, "read", "beta/prod/K", None, None, None, None, wait=0)
    fake.slow = 0.15
    out, err = io.StringIO(), io.StringIO()
    jobs = [threading.Thread(target=m.cmd_issue, args=("read", "alpha/prod/K", None, None, None, None),
                             kwargs={"wait": 0}),
            threading.Thread(target=m.cmd_issue, args=("deploy", "gamma/prod/K", None, None, None, None),
                             kwargs={"wait": 0}),
            threading.Thread(target=m.cmd_rotate, args=("beta/prod/K", False), kwargs={"wait": 0})]
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        for t in jobs:
            t.start()
        for t in jobs:
            t.join(30)
    fake.slow = 0
    rows = json.loads(m.LEDGER.read_text())["issued"]
    check("all three rows survive concurrent runs", sorted(rows) == ["alpha/prod/K", "beta/prod/K", "gamma/prod/K"],
          str(sorted(rows)) + err.getvalue())
    check("every row names a live authorization", all(r["authorization_id"] in fake.auths for r in rows.values()),
          str({k: r["authorization_id"] for k, r in rows.items()}) + " live " + str(sorted(fake.auths)))
    check("and the rotate counted on its own row", rows["beta/prod/K"]["rotations"] == 1, str(rows["beta/prod/K"]))
    rc, o, e = run(m.cmd_ping)
    check("ping then finds every row alive and nothing stray", rc == 0 and "stray" not in o, o + e)


def test_a_held_lock_makes_another_process_wait_then_refuse() -> None:
    """The lock is a file lock, so it binds another process too: while one holds it,
    an `issue` waits LOCK_WAIT_S and then refuses having minted nothing."""
    import subprocess as sp
    m, fake, delivered = setup()
    m.LEDGER.parent.mkdir(parents=True, exist_ok=True)
    holder = sp.Popen([sys.executable, "-c", (
        "import os, sys, time; sys.path.insert(0, sys.argv[1]); import oslocks; "
        "fd = os.open(sys.argv[2], os.O_RDWR | os.O_CREAT, 0o600); oslocks.flock(fd, oslocks.LOCK_EX); "
        "print('held', flush=True); time.sleep(5)"),
        str(ROOT), str(m.LEDGER.parent / ".heroku-ledger.lock")], stdout=sp.PIPE, text=True)
    try:
        check("the other process holds the lock", holder.stdout.readline().strip() == "held", "")
        m.LOCK_WAIT_S = 0.3
        rc, o, e = run(m.cmd_issue, "read", "alpha/prod/K", None, None, None, None, wait=0)
        check("issue refuses after waiting", rc == 1 and "ledger lock" in e, o + e)
        check("and minted nothing", not fake.auths and not delivered, str(fake.auths))
    finally:
        holder.kill()
        holder.wait()


def test_issue_proves_every_target_it_names_and_claims_no_more() -> None:
    m, fake, delivered = setup()
    rc, o, e = run(m.cmd_issue, "read", "alpha/prod/K", "example-team", "alpha-web", None, None, wait=0)
    new = next(iter(fake.auths.values()))["token"]
    read = {p for _m, p, t, _x in fake.calls if t == new}
    check("with --team and --app both are read by the new value",
          rc == 0 and {"/apps/alpha-web", "/teams/example-team/apps"} <= read, str(read))
    check("and the output names both as proved",
          "it read app alpha-web, team example-team's apps" in o and "cannot read them" not in o, o)
    m, fake, delivered = setup()
    rc, o, e = run(m.cmd_issue, "identity", "alpha/prod/K", "example-team", None, None, None, wait=0)
    check("an identity value proves the account only, and says the team was not proved by it",
          rc == 0 and "it read the account" in o and "team example-team exist for the admin but this scope "
          "cannot read them" in o, o)
    m, fake, delivered = setup()
    fake.apps["example-team"] = []
    rc, o, e = run(m.cmd_issue, "read", "alpha/prod/K", "example-team", None, None, None, wait=0)
    check("a team proof reads the team's apps even when it has none", rc == 0, o + e)


def test_ping_counts_authorizations_per_admin() -> None:
    m, fake, delivered = setup()
    run(m.cmd_issue, "read", "alpha/prod/K", None, None, None, None, wait=0)
    private_io.write(m.ADMIN_STORE / "second-admin", ADMIN + "\n")
    rc, o, e = run(m.cmd_ping)
    counts = [ln for ln in o.splitlines() if ln.strip().startswith("admin/")]
    check("each admin line counts its own authorizations, not a running total",
          len(counts) == 2 and all("alive, 1 authorization(s)" in ln for ln in counts), o)


def test_a_dropped_connection_is_named_not_raised_raw() -> None:
    import http.client
    m = door()

    def drop(req, timeout):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")
    try:
        with patched(m.urllib.request, "urlopen", drop):
            m._request("GET", "/teams", ADMIN)
        check("a dropped connection raises", False, "no exception")
    except m.HerokuError as exc:
        check("a dropped connection is a named transport failure",
              exc.status == "transport" and "RemoteDisconnected" in str(exc), str(exc))


def test_errors_carry_status_and_endpoint_only() -> None:
    m = door()
    secret_body = b'{"id":"forbidden","message":"echo HRKU-leaked-in-body"}'

    def boom(req, timeout):
        check("the bearer header is the token, sent in a header not the URL",
              req.get_header("Authorization") == "Bearer " + ADMIN and ADMIN not in req.full_url, "")
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(secret_body))
    try:
        with patched(m.urllib.request, "urlopen", boom):
            m._request("GET", "/teams/example-team/members", ADMIN)
        check("a refusal raises", False, "no exception")
    except m.HerokuError as exc:
        check("the error names status and endpoint shape only",
              exc.status == 403 and "/teams/<id>/members" in str(exc)
              and "HRKU" not in str(exc) and "example-team" not in str(exc), str(exc))


def test_delivery_goes_through_the_vault_on_stdin() -> None:
    m = door()
    calls = []

    class Done:
        returncode, stdout, stderr = 3, "ECHOED-INPUT", "ECHOED-INPUT"

    try:
        with patched(m.subprocess, "run", lambda argv, **kw: (calls.append((argv, kw)), Done())[1]):
            m.deliver_to_vault("HRKU-synthetic-" + "v" * 30, "alpha", "prod", "K")
        check("a vault refusal raises", False, "no exception")
    except RuntimeError as exc:
        argv, kw = calls[0]
        check("the vault's put is called for the slot", argv[2:] == ["put", "alpha", "prod", "K", "--force"], str(argv))
        check("the value travels on stdin, not argv", kw.get("input", "").startswith("HRKU-synthetic-")
              and not any("HRKU" in str(a) for a in argv), "")
        check("and the refusal withholds the child's output", "ECHOED" not in str(exc) and "HRKU" not in str(exc), str(exc))


def test_release_deploy_proves_protected_endpoints() -> None:
    m, fake, delivered = setup()
    check("release-deploy is a separate explicit preset", "release-deploy" in m.PRESETS)
    check("legacy deploy retains its original scopes", m.PRESETS["deploy"]["scopes"] == ("read", "write"))
    if "release-deploy" not in m.PRESETS:
        return
    m._release_read = lambda path, value, range_header=None: fake("GET", path, value, rng=range_header)[0]
    check("release-deploy discloses protected configuration capability",
          "config" in m.PRESETS["release-deploy"]["why"].lower())
    rc, o, e = run(m.cmd_issue, "release-deploy", "alpha/prod/K", None, None, None, 7, wait=0)
    check("release-deploy requires an app before minting", rc == 2 and not fake.auths and not delivered)
    rc, o, e = run(m.cmd_issue, "release-deploy", "alpha/prod/K", None, "alpha-web", None, 7, wait=0)
    check("release-deploy issue succeeds after all reads", rc == 0, o + e)
    new = delivered[-1][0] if delivered else None
    paths = [path for method, path, token, payload in fake.calls if method == "GET" and token == new]
    check("proof covers exact app and protected release resources",
          paths == ["/apps/alpha-web", "/apps/alpha-web/builds", "/apps/alpha-web/releases", "/apps/alpha-web/slugs/12345678-1234-1234-1234-123456789abc"], str(paths))
    check("proof never reads config vars or makes app writes",
          all("config-vars" not in path and not (path.startswith("/apps/") and method != "GET")
              for method, path, token, payload in fake.calls))
    check("protected scopes are explicit, never global",
          next(iter(fake.auths.values()))["scope"] == ["read-protected", "write-protected"])
    check("successful issue does not print a value", no_value(o + e))
    for endpoint in ("releases", "builds", "slugs"):
        m, fake, delivered = setup()
        m._release_read = lambda path, value, range_header=None: fake("GET", path, value, rng=range_header)[0]
        fake.refuse["/apps/alpha-web/" + endpoint] = 403
        rc, o, e = run(m.cmd_issue, "release-deploy", "alpha/prod/K", None, "alpha-web", None, 7, wait=0)
        check("failed " + endpoint + " proof prevents delivery and removes fresh authorization",
              rc == 1 and not fake.auths and not delivered and not m.LEDGER.exists(), o + e)
        check("failed " + endpoint + " proof emits no credential", no_value(o + e))


def test_release_proof_rejects_missing_or_unsafe_slug() -> None:
    for response in ([], [{"slug": None}], [{"slug": {"id": "../config-vars"}}], {},
                     [{"slug": {"id": "x"}}] * 2):
        m, fake, delivered = setup()
        if "release-deploy" not in m.PRESETS:
            check("closed release proof is implemented", False)
            return
        def read(path, value, range_header=None):
            if path.endswith("/releases"):
                return response
            return fake("GET", path, value, rng=range_header)[0]
        m._release_read = read
        rc, o, e = run(m.cmd_issue, "release-deploy", "alpha/prod/K", None, "alpha-web", None, 7, wait=0)
        check("invalid or unavailable slug proof removes fresh credential",
              rc == 1 and not delivered and not fake.auths and no_value(o + e))


def test_protected_release_wire_is_bounded_and_refuses_redirects() -> None:
    m = door()
    if not hasattr(m, "_release_read") or not hasattr(m, "_NoReleaseRedirect"):
        check("protected release read transport exists", False)
        return
    check("credential redirect is refused", m._NoReleaseRedirect().redirect_request(
        None, None, 302, "", {}, "https://outside.example.invalid/") is None)
    seen = []
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, n):
            seen.append(n)
            return b"x" * n
    class Opener:
        def open(self, request, timeout):
            check("protected request uses fixed origin and header credential",
                  request.full_url == "https://api.heroku.com/apps/alpha-web/releases"
                  and request.get_header("Authorization") == "Bearer " + ADMIN)
            return Reply()
    try:
        with patched(m.urllib.request, "build_opener", lambda *args: Opener()):
            m._release_read("/apps/alpha-web/releases", ADMIN)
        check("oversized protected response is refused", False)
    except m.HerokuError as exc:
        check("oversized response is typed and value-free", no_value(str(exc)))
    check("protected body read has a finite bound", seen == [1024 * 1024 + 1])


@contextlib.contextmanager
def no_network():
    """A tripwire: every way out of this process to a socket raises and is recorded,
    so a release proof that bypassed the fake would fail here, not reach Heroku."""
    import http.client
    import socket
    tripped: list[str] = []

    def trip(name):
        def refuse(*_a, **_k):
            tripped.append(name)
            raise AssertionError(f"network reached through {name}")
        return refuse
    with contextlib.ExitStack() as stack:
        for obj, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                          (socket, "create_connection"), (http.client.HTTPConnection, "connect"),
                          (http.client.HTTPSConnection, "connect"), (urllib.request, "urlopen")):
            stack.enter_context(patched(obj, name, trip(f"{getattr(obj, '__name__', obj)}.{name}")))
        yield tripped


def release_setup(vault_ok=True):
    """`setup` with the protected transport routed to the fake, recording the Range
    each read carried and how many calls had been made when the vault was handed a value."""
    m, fake, delivered = setup(vault_ok=vault_ok)
    if hasattr(m, "_release_read"):
        m._release_read = lambda path, value, range_header=None: fake("GET", path, value, rng=range_header)[0]
    order: list[int] = []
    inner = m.deliver_to_vault

    def deliver(value, project, env, name):
        order.append(len(fake.calls))
        return inner(value, project, env, name)
    m.deliver_to_vault = deliver
    return m, fake, delivered, order


PROOF_PATHS = ["/apps/alpha-web", "/apps/alpha-web/builds", "/apps/alpha-web/releases",
               "/apps/alpha-web/slugs/" + SLUG_ID]


def reads_by(fake, token, since=0):
    return [path for method, path, tok, _p in fake.calls[since:] if method == "GET" and tok == token]


def test_release_deploy_rotation_proves_with_the_regenerated_value() -> None:
    with no_network() as tripped:
        m, fake, delivered, order = release_setup()
        if "release-deploy" not in m.PRESETS:
            check("rotation of release-deploy runs the release proof", False, "no release-deploy preset")
            return
        rc, o, e = run(m.cmd_issue, "release-deploy", "alpha/prod/K", None, "alpha-web", None, 7, wait=0)
        first = delivered[-1][0] if delivered else None
        since = len(fake.calls)
        rc, o, e = run(m.cmd_rotate, "alpha/prod/K", False, wait=0)
        new = next(iter(fake.auths.values()))["token"]
        check("rotate of release-deploy delivers the regenerated value", rc == 0 and len(delivered) == 2
              and delivered[-1][0] == new and new != first, o + e)
        check("the regenerated value reads the app, builds, newest release and its exact slug",
              reads_by(fake, new, since) == PROOF_PATHS, str(reads_by(fake, new, since)))
        check("the old value is used for no proof read after rotation", not reads_by(fake, first, since), "")
        proof_end = max(i for i, (meth, path, tok, _p) in enumerate(fake.calls) if tok == new)
        check("every proof read happens before the vault is handed the value",
              order and order[-1] > proof_end, f"{order} {proof_end}")
        check("the rotation's release read carried the pinned Range",
              fake.ranges and fake.ranges[-1] == ("/apps/alpha-web/releases", RELEASE_RANGE), str(fake.ranges))
        row = json.loads(m.LEDGER.read_text())["issued"]["alpha/prod/K"]
        check("and the ledger counts one rotation of the release-deploy row",
              row["rotations"] == 1 and row["preset"] == "release-deploy", str(row))
        check("rotation prints no value", no_value(o + e), o + e)

        for refused in ("/apps/alpha-web/releases", "/apps/alpha-web/slugs"):
            m, fake, delivered, order = release_setup()
            run(m.cmd_issue, "release-deploy", "alpha/prod/K", None, "alpha-web", None, 7, wait=0)
            fake.refuse[refused] = 403
            rc, o, e = run(m.cmd_rotate, "alpha/prod/K", False, wait=0)
            row = json.loads(m.LEDGER.read_text())["issued"]["alpha/prod/K"]
            check(f"a protected 403 on {refused} after regenerate says the old value is dead",
                  rc == 1 and "REGENERATED but not delivered" in e and "old value is dead" in e, e)
            check(f"and delivers nothing and counts no rotation ({refused})",
                  len(delivered) == 1 and row["rotations"] == 0, str(row))
            check(f"and prints no value ({refused})", no_value(o + e), o + e)

        m, fake, delivered, order = release_setup()
        run(m.cmd_issue, "release-deploy", "alpha/prod/K", None, "alpha-web", None, 7, wait=0)
        since = len(fake.calls)

        def refuse_vault(*_a):
            raise RuntimeError("vault refused the slot alpha/prod/K (exit 3); child output withheld")
        m.deliver_to_vault = refuse_vault
        rc, o, e = run(m.cmd_rotate, "alpha/prod/K", False, wait=0)
        new = next(iter(fake.auths.values()))["token"]
        row = json.loads(m.LEDGER.read_text())["issued"]["alpha/prod/K"]
        check("a vault failure after a full release proof says the old value is dead",
              rc == 1 and reads_by(fake, new, since) == PROOF_PATHS and "old value is dead" in e
              and row["rotations"] == 0 and no_value(o + e), o + e)
    check("rotation tests reached no network", not tripped, str(tripped))


def test_release_read_pins_the_newest_release_range() -> None:
    with no_network() as tripped:
        m, fake, delivered, order = release_setup()
        if "release-deploy" not in m.PRESETS:
            check("the release Range is pinned", False, "no release-deploy preset")
            return
        check("the door pins a descending one-release Range", getattr(m, "RELEASE_RANGE", None) == RELEASE_RANGE,
              str(getattr(m, "RELEASE_RANGE", None)))
        rc, o, e = run(m.cmd_issue, "release-deploy", "alpha/prod/K", None, "alpha-web", None, 7, wait=0)
        check("issue reads releases once, with exactly that Range",
              rc == 0 and fake.ranges == [("/apps/alpha-web/releases", RELEASE_RANGE)], f"{fake.ranges} {o}{e}")
        new = delivered[-1][0] if delivered else None
        check("and proves the newest release's slug, never an older one",
              reads_by(fake, new)[-1:] == ["/apps/alpha-web/slugs/" + SLUG_ID]
              and not any(OLD_SLUG_ID in p for _m, p, _t, _x in fake.calls), str(reads_by(fake, new)))
    check("range tests reached no network", not tripped, str(tripped))


def test_release_proof_refuses_a_slug_id_that_is_not_exactly_a_uuid() -> None:
    with no_network() as tripped:
        for bad in (SLUG_ID + "\n", "\n" + SLUG_ID, 12345678, 1.5, True, [SLUG_ID], {"id": SLUG_ID},
                    SLUG_ID + "0" * 4096, "a" * 100000, SLUG_ID.replace("-", ""), SLUG_ID[:-1] + "g"):
            m, fake, delivered, order = release_setup()
            if "release-deploy" not in m.PRESETS:
                check("an unsafe slug id is refused", False, "no release-deploy preset")
                return

            def read(path, value, range_header=None, bad=bad, fake=fake):
                if path.endswith("/releases"):
                    fake("GET", path, value, rng=range_header)
                    return [{"version": 7, "slug": {"id": bad}}]
                return fake("GET", path, value, rng=range_header)[0]
            m._release_read = read
            rc, o, e = run(m.cmd_issue, "release-deploy", "alpha/prod/K", None, "alpha-web", None, 7, wait=0)
            check(f"slug id {type(bad).__name__} of length {len(str(bad))} is refused before any slug read",
                  rc == 1 and not any("/slugs" in p for _m, p, _t, _x in fake.calls), o + e)
            check(f"and before delivery, with the fresh authorization deleted ({type(bad).__name__})",
                  not delivered and not fake.auths and not m.LEDGER.exists() and no_value(o + e), o + e)
            check("and the refusal does not echo the offered id",
                  not isinstance(bad, str) or len(bad) < 40 or bad.strip() not in o + e, "")
    check("slug id tests reached no network", not tripped, str(tripped))


def test_protected_release_wire_installs_its_guards_and_types_every_failure() -> None:
    import http.client
    m = door()
    if not hasattr(m, "_release_read") or not hasattr(m, "_NoReleaseRedirect"):
        check("protected release read transport exists", False)
        return
    secret_body = b'{"id":"forbidden","message":"echo HRKU-leaked-in-body"}'

    class Reply:
        def __init__(self, body=b"[]", fail=None):
            self.body, self.fail = body, fail
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, n):
            if self.fail:
                raise self.fail
            return self.body[:n]
    handlers: list = []
    requests: list = []
    outcome: dict = {}

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            what = outcome["next"]
            if isinstance(what, BaseException):
                raise what
            return what

    def build(*args):
        handlers.append(args)
        return Opener()
    cases = [
        ("HTTP 403 with a body naming a value", urllib.error.HTTPError(
            "https://api.heroku.com/apps/alpha-web/releases", 403, "Forbidden", {}, io.BytesIO(secret_body)), 403),
        ("a redirect the handler refused surfaces as its status", urllib.error.HTTPError(
            "https://api.heroku.com/apps/alpha-web/releases", 302, "Found",
            {"Location": "https://outside.example.invalid/"}, io.BytesIO(b"")), 302),
        ("malformed UTF-8", Reply(b"\xff\xfe\xfd"), "shape"),
        ("malformed JSON", Reply(b'{"not json'), "shape"),
        ("an empty body", Reply(b""), "shape"),
        ("a connection dropped before the answer", http.client.RemoteDisconnected("closed"), "transport"),
        ("a connection dropped mid-body", Reply(fail=http.client.IncompleteRead(b"[")), "transport"),
        ("a reset connection", ConnectionResetError("reset"), "transport"),
        ("a timeout", TimeoutError("timed out"), "transport"),
    ]
    with no_network() as tripped, patched(m.urllib.request, "build_opener", build):
        for label, what, status in cases:
            outcome["next"] = what
            try:
                m._release_read("/apps/alpha-web/releases", ADMIN, m.RELEASE_RANGE)
                check(f"{label} raises", False, "no exception")
            except m.HerokuError as exc:
                check(f"{label} is a typed failure ({status})", exc.status == status, f"{exc.status} {exc}")
                check(f"{label} is value-free", no_value(str(exc)) and "outside.example" not in str(exc)
                      and "alpha-web" not in str(exc), str(exc))
            except Exception as exc:                                  # an untyped escape is the failure
                check(f"{label} is a typed failure ({status})", False, type(exc).__name__)
        check("every protected open installs the no-redirect handler",
              handlers and all(any(h is m._NoReleaseRedirect or isinstance(h, m._NoReleaseRedirect) for h in args)
                               for args in handlers), str(handlers))
        check("the handler refuses to follow a redirect", m._NoReleaseRedirect().redirect_request(
            None, None, 302, "", {}, "https://outside.example.invalid/") is None)
        check("each request goes to the fixed origin with the credential in a header and the pinned Range",
              all(r.full_url == "https://api.heroku.com/apps/alpha-web/releases"
                  and r.get_header("Authorization") == "Bearer " + ADMIN
                  and r.get_header("Range") == m.RELEASE_RANGE for r in requests), "")
        opened = len(requests)
        for path in ("https://outside.example.invalid/apps/x", "//outside.example.invalid/apps",
                     "/apps/alpha-web/config-vars", "/account"):
            try:
                m._release_read(path, ADMIN)
                check(f"{path} is refused", False, "no exception")
            except m.HerokuError as exc:
                check(f"a path outside the release reads is refused before opening ({path})",
                      len(requests) == opened and no_value(str(exc)), str(exc))
    check("transport tests reached no network", not tripped, str(tripped))


def test_release_vault_failure_after_proof_deletes_the_fresh_authorization() -> None:
    with no_network() as tripped:
        m, fake, delivered, order = release_setup(vault_ok=False)
        if "release-deploy" not in m.PRESETS:
            check("a vault failure after proof cleans up", False, "no release-deploy preset")
            return
        rc, o, e = run(m.cmd_issue, "release-deploy", "alpha/prod/K", None, "alpha-web", None, 7, wait=0)
        minted = [p for meth, p, _t, _x in fake.calls if meth == "POST" and p == "/oauth/authorizations"]
        new_reads = [p for meth, p, tok, _x in fake.calls if meth == "GET" and tok not in (ADMIN,)]
        check("the full proof ran before the vault refused", new_reads == PROOF_PATHS, str(new_reads))
        check("the vault refusal fails the issue and deletes the fresh authorization",
              rc == 1 and len(minted) == 1 and not fake.auths and "deleted again" in e, o + e)
        check("and writes no ledger row", not m.LEDGER.exists(), "")
        check("and prints no value", no_value(o + e), o + e)

        m, fake, delivered, order = release_setup()
        run(m.cmd_issue, "read", "alpha/prod/K", None, "alpha-web", None, None, wait=0)

        def refuse_vault(*_a):
            raise RuntimeError("vault refused the slot alpha/prod/K (exit 3); child output withheld")
        m.deliver_to_vault = refuse_vault
        rc, o, e = run(m.cmd_issue, "release-deploy", "alpha/prod/K", None, "alpha-web", None, 7, wait=0)
        row = json.loads(m.LEDGER.read_text())["issued"]["alpha/prod/K"]
        check("over an existing slot, the slot's previous authorization survives a failed delivery",
              rc == 1 and list(fake.auths) == ["auth-0001-0000"] and row["authorization_id"] == "auth-0001-0000"
              and row["preset"] == "read", f"{list(fake.auths)} {row}")
        check("and the fresh one is deleted again", "auth-0002-0000" not in fake.auths and "deleted again" in e, e)
    check("cleanup tests reached no network", not tripped, str(tripped))


def test_legacy_deploy_claims_no_release_access() -> None:
    m = door()
    why = m.PRESETS["deploy"]["why"]
    check("legacy deploy keeps read and write", m.PRESETS["deploy"]["scopes"] == ("read", "write"), "")
    check("legacy deploy no longer claims releases it never proves",
          "(deploys, scaling, releases)" not in why and "release-deploy" in why, why)


def test_the_door_source_keeps_the_rules() -> None:
    src = (ROOT / "tools" / "heroku.py").read_text(encoding="utf-8")
    check("values arrive on stdin", "sys.stdin.read()" in src and '"--token"' not in src, "")
    check("provider errors are stripped", "from None" in src and "e.read()" not in src, "")
    check("the refresh token is never stored", "refresh_token" not in src.split('"""', 2)[2], "")


if __name__ == "__main__":
    print("the Heroku door: stash, issue, rotate, revoke, ping, teams and membership writes\n")
    for fn in (test_stash_keeps_the_admin_private_and_refuses_what_cannot_issue,
               test_stash_from_cli_reads_the_session_token_without_argv,
               test_issue_mints_a_narrow_authorization_proves_it_and_delivers_it,
               test_issue_refuses_before_minting,
               test_issue_deletes_what_it_created_when_proof_or_delivery_fails,
               test_issue_twice_regenerates_in_place_and_a_new_preset_retires_the_old,
               test_rotate_regenerates_proves_and_delivers,
               test_rotate_leaked_reads_the_vault_register,
               test_revoke_deletes_at_heroku_before_the_ledger,
               test_ping_and_list_report_without_values,
               test_teams_members_and_apps_read_with_their_slots,
               test_member_writes_are_a_plan_until_apply_and_are_verified,
               test_member_writes_refuse_what_would_lock_the_team,
               test_a_team_that_invites_gets_invitations_the_cli_way,
               test_concurrent_door_runs_keep_every_ledger_row,
               test_a_held_lock_makes_another_process_wait_then_refuse,
               test_issue_proves_every_target_it_names_and_claims_no_more,
               test_ping_counts_authorizations_per_admin,
               test_a_dropped_connection_is_named_not_raised_raw,
               test_errors_carry_status_and_endpoint_only,
               test_delivery_goes_through_the_vault_on_stdin,
               test_release_deploy_proves_protected_endpoints,
               test_release_proof_rejects_missing_or_unsafe_slug,
               test_protected_release_wire_is_bounded_and_refuses_redirects,
               test_release_deploy_rotation_proves_with_the_regenerated_value,
               test_release_read_pins_the_newest_release_range,
               test_release_proof_refuses_a_slug_id_that_is_not_exactly_a_uuid,
               test_protected_release_wire_installs_its_guards_and_types_every_failure,
               test_release_vault_failure_after_proof_deletes_the_fresh_authorization,
               test_legacy_deploy_claims_no_release_access,
               test_the_door_source_keeps_the_rules):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} failed")
        for f in FAILURES:
            print("  -", f)
        raise SystemExit(1)
    print("heroku door checks passed")
