#!/usr/bin/env python3
"""The Sentry door (`tools/sentry.py`, OBS-292) against a fake Sentry on loopback.

Nothing here reaches sentry.io. Two local HTTP servers play Sentry's control host (`/api/0/`,
organizations, organization tokens) and the organization's region host (releases, issues,
members), with the permission rules read from Sentry's source: an organization token is minted
only for an OAuth application's token holding `org:tokens`, only with `org:ci`; a personal token
mints nothing. The vault is a temporary one, written and read through the vault's own code.

What is asserted is the door's reasoning:
- a value never reaches argv, a URL, the output, the ledger, the slot's metadata or the journal;
- a personal token, and the `read` preset, are refused before any call, with the page and the
  `install` that follows;
- a minted token is proved with its own value (exact scopes, the purpose read on the REGION host,
  the reads outside it refused) before the vault gets it, and deleted again when it fails;
- the broad token is retired only after a narrow one verifies, and only a 401 counts as revoked;
- hosts: https or loopback only, a region under the base's domain only.
"""
from __future__ import annotations
import base64
import contextlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
import sys
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir  # noqa: E402  sandboxes HOME first

os.environ["OBSERVATORY_VAULT_DIR"] = str(pathlib.Path(tmpdir.mkdtemp("sentry-vault-")).resolve())
sys.path.insert(0, str(ROOT / "tools"))
import vault  # noqa: E402
import use_secret  # noqa: E402

ORG = "example-org"
SITE = "example-site"
ADMIN_SLOT = ("example-site", "prod", "SENTRY_ADMIN_TOKEN")
NARROW_SLOT = ("example-site", "prod", "SENTRY_RELEASES_TOKEN")
READ_SLOT = ("example-site", "prod", "SENTRY_READ_TOKEN")
SEEN: list[str] = []


def fake(prefix: str, label: str) -> str:
    """An obviously synthetic token: the kind prefix, then words, never a real token's shape."""
    value = f"{prefix}test-only-{label}-not-a-credential"
    SEEN.append(value)
    return value


def fake_org_token(n: int, control: str, region: str, org: str = ORG) -> str:
    """The organization token's shape, built at run time: `sntrys_<base64 payload>_<secret>`."""
    payload = base64.b64encode(json.dumps({"iat": 1, "url": control, "region_url": region,
                                           "org": org}).encode()).decode()
    value = f"sntrys_{payload}_testonly{n:02d}notacredential"
    SEEN.append(value)
    return value


class State:
    def __init__(self):
        self.tokens: dict[str, dict] = {}
        self.requests: list[tuple[str, str, str, str, object]] = []
        self.control = self.region = ""
        self.n = 0
        self.members_open = False
        self.refuse: dict[str, int] = {}
        self.lock = threading.Lock()

    def add(self, value: str, kind: str, scopes, **extra) -> str:
        self.tokens[value] = {"kind": kind, "scopes": set(scopes), "alive": True, **extra}
        return value


def make_handler(state: State, role: str):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status: int, body=None):
            raw = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _do(self, method: str):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            auth = self.headers.get("Authorization", "")
            value = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
            path = self.path.split("?")[0]
            with state.lock:
                state.requests.append((role, method, self.path, value, body))
            for prefix, status in state.refuse.items():
                if path.startswith(prefix):
                    # The provider's body echoes the request: the door must never print it.
                    return self._send(status, {"detail": f"echo {value}"})
            tok = state.tokens.get(value)
            if not tok or not tok["alive"]:
                return self._send(401, {"detail": "Invalid token"})
            sc = tok["scopes"]
            # Sentry's hierarchy: org:write and org:admin include org:read.
            org_read = bool(sc & {"org:read", "org:write", "org:admin"})
            parts = [p for p in path.split("/") if p]
            if role == "control":
                if path == "/api/0/" and method == "GET":
                    return self._send(200, {"version": "0", "auth": {"scopes": sorted(sc)}, "user": None})
                if path == "/api/0/organizations/" and method == "GET":
                    return self._send(200 if org_read else 403, [{"slug": ORG}] if org_read else {"detail": "no"})
                if parts[:3] == ["api", "0", "organizations"] and len(parts) == 4 and method == "GET":
                    if not org_read:
                        return self._send(403, {"detail": "no"})
                    return self._send(200, {"slug": parts[3], "links": {"regionUrl": state.region,
                                                                         "organizationUrl": state.control}})
                if parts[:3] == ["api", "0", "organizations"] and len(parts) >= 5 and parts[4] == "org-auth-tokens":
                    # OrgAuthTokenPermission: an OAuth application's token with org:tokens, nothing else.
                    if tok["kind"] != "oauth-app" or "org:tokens" not in sc:
                        return self._send(403, {"detail": "You do not have permission"})
                    if method == "POST" and len(parts) == 5:
                        if not sc & {"org:read", "org:write", "org:admin"}:
                            return self._send(403, {"detail": "no"})
                        if set(body.get("scopes") or []) - {"org:ci"}:
                            return self._send(400, {"detail": "Org auth tokens cannot be granted"})
                        state.n += 1
                        value_new = fake_org_token(state.n, state.control, state.region, parts[3])
                        state.add(value_new, "org", body["scopes"], id=str(state.n), name=body.get("name"))
                        return self._send(201, {"id": str(state.n), "name": body.get("name"),
                                                "scopes": body["scopes"], "token": value_new,
                                                "tokenLastCharacters": value_new[-4:]})
                    if method == "DELETE" and len(parts) == 6:
                        if not sc & {"org:write", "org:admin"}:
                            return self._send(403, {"detail": "no"})
                        hit = next((t for t in state.tokens.values() if t.get("id") == parts[5] and t["alive"]), None)
                        if not hit:
                            return self._send(404, {"detail": "not found"})
                        hit["alive"] = False
                        return self._send(204)
                return self._send(404, {"detail": f"not a control path {path}"})
            # region host
            if parts[-1] == "releases" and method == "GET":
                ok = sc & {"org:ci", "project:releases", "project:read", "project:admin"}
                return self._send(200 if ok else 403, [] if ok else {"detail": "no"})
            if parts[-1] == "issues" and method == "GET":
                ok = sc & {"event:read", "event:admin"}
                return self._send(200 if ok else 403, [] if ok else {"detail": "no"})
            if parts[-1] == "members" and method == "GET":
                ok = state.members_open or sc & {"member:read", "member:admin"}
                return self._send(200 if ok else 403, [] if ok else {"detail": "no"})
            return self._send(404, {"detail": f"not a region path {path}"})

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

        def do_DELETE(self):
            self._do("DELETE")
    return Handler


def door():
    spec = importlib.util.spec_from_file_location("sentry_door", ROOT / "tools" / "sentry.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.LEDGER = pathlib.Path(tmpdir.mkdtemp("sentry-ledger-")).resolve() / "sentry-issued.json"
    return m


def put(slot, value):
    if slot_value(slot) is not None:
        vault.store_value(*slot, value, replace=True, tool="test")
    else:
        vault.store_value(*slot, value, replace=False, tool="test")


def slot_value(slot):
    found, _env = use_secret.vault_slot(slot[0], slot[2], slot[1])
    return found.read_text(encoding="utf-8").strip() if found else None


def clear(slot):
    found, _env = use_secret.vault_slot(slot[0], slot[2], slot[1])
    if found:
        found.unlink()
        found.with_name(found.name + ".meta.json").unlink(missing_ok=True)


def run(fn, *a, **k):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = fn(*a, **k)
        except Exception as exc:  # the CLI's own handler is main(); here refusals are caught alike
            rc = getattr(exc, "code", 99)
            print(f"refused: {exc}", file=sys.stderr)
    return rc, out.getvalue(), err.getvalue()


def no_value(text: str) -> bool:
    return not any(v in text for v in SEEN) and "notacredential" not in text and "not-a-credential" not in text


class SentryDoor(unittest.TestCase):
    def setUp(self):
        self.m = door()
        self.state = State()
        self.servers = []
        for role in ("control", "region"):
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state, role))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.servers.append(srv)
            setattr(self.state, role, f"http://127.0.0.1:{srv.server_address[1]}")
        self.base = self.state.control
        # The fake control host plays sentry.io: its documented region host is the fake region server.
        self.m.KNOWN_REGIONS = {self.base: (self.state.region,)}
        self.argv: list[list[str]] = []
        real_run = self.m.subprocess.run

        def recording_run(argv, **kw):
            self.argv.append(list(argv))
            return real_run(argv, **kw)
        self.m.subprocess = type("S", (), {"run": staticmethod(recording_run)})
        for slot in (ADMIN_SLOT, NARROW_SLOT, READ_SLOT):
            clear(slot)

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    # helpers
    def oauth_admin(self, scopes=("org:read", "org:tokens", "org:write")):
        value = self.state.add(fake("sntrya_", "oauth-admin-" + "-".join(x.replace(":", "") for x in scopes)),
                               "oauth-app", scopes)
        put(ADMIN_SLOT, value)
        return value

    def personal_admin(self):
        value = self.state.add(fake("sntryu_", "personal-admin"), "personal",
                               ("org:admin", "member:admin", "team:admin", "project:admin", "event:admin",
                                "org:read", "member:read", "event:read", "project:read"))
        put(ADMIN_SLOT, value)
        return value

    def issue(self, **kw):
        args = dict(preset_key="releases", target="/".join(NARROW_SLOT), source="/".join(ADMIN_SLOT), org=None,
                    project=SITE, base_url=self.base, region=None, region_url=None, wait=0)
        args.update(kw)
        return run(self.m.cmd_issue, **args)

    def surfaces(self, out: str) -> str:
        """Everything a person, a log or Git could read: output, ledger, slot metadata, the journal."""
        texts = [out]
        if self.m.LEDGER.exists():
            texts.append(self.m.LEDGER.read_text(encoding="utf-8"))
        store = pathlib.Path(os.environ["OBSERVATORY_VAULT_DIR"])
        texts += [p.read_text(encoding="utf-8") for p in store.rglob("*.meta.json")]
        if vault.MOVES.exists():
            texts.append(vault.MOVES.read_text(encoding="utf-8"))
        return "\n".join(texts)

    # ── issue ──────────────────────────────────────────────────────────────────

    def test_issue_mints_org_ci_verifies_on_the_region_and_delivers(self):
        admin = self.oauth_admin()
        rc, o, e = self.issue()
        self.assertEqual(rc, 0, o + e)
        posts = [r for r in self.state.requests if r[1] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][4]["scopes"], ["org:ci"])
        self.assertEqual(posts[0][4]["name"], "observatory releases example-site/prod/SENTRY_RELEASES_TOKEN (managed)")
        new = slot_value(NARROW_SLOT)
        self.assertTrue(new and new.startswith("sntrys_") and new != admin)
        own = [(r[0], r[2]) for r in self.state.requests if r[3] == new]
        self.assertIn(("control", "/api/0/"), own)
        self.assertIn(("region", f"/api/0/projects/{ORG}/{SITE}/releases/?per_page=1"), own)
        self.assertIn(("region", f"/api/0/organizations/{ORG}/members/?per_page=1"), own)
        self.assertIn(("region", f"/api/0/organizations/{ORG}/issues/?limit=1"), own)
        row = json.loads(self.m.LEDGER.read_text())["issued"]["example-site/prod/SENTRY_RELEASES_TOKEN"]
        self.assertEqual((row["preset"], row["kind"], row["scopes"], row["org"], row["project"], row["token_id"],
                          row["minted_by"], row["from"], row["region_url"]),
                         ("releases", "org", ["org:ci"], ORG, SITE, "1", "door",
                          "example-site/prod/SENTRY_ADMIN_TOKEN", self.state.region))
        self.assertIsNone(row["expires_at"])
        self.assertIn("no expiry", row["expiry_reason"])
        meta = json.loads(next(pathlib.Path(os.environ["OBSERVATORY_VAULT_DIR"]).rglob(
            "SENTRY_RELEASES_TOKEN.meta.json")).read_text())
        self.assertEqual(meta.get("expiry_by"), "sentry.py")
        self.assertTrue(no_value(self.surfaces(o + e)), o + e)
        self.assertTrue(slot_value(ADMIN_SLOT) == admin, "the broad token stays until revoke --from")

    def test_values_never_travel_in_argv_or_a_url(self):
        self.oauth_admin()
        rc, o, e = self.issue()
        self.assertEqual(rc, 0, o + e)
        self.assertTrue(self.argv, "the vault was written through its own program")
        self.assertTrue(all(no_value(" ".join(a)) for a in self.argv), self.argv)
        self.assertTrue(all(no_value(r[2]) for r in self.state.requests))

    def test_a_personal_token_is_refused_before_any_call(self):
        self.personal_admin()
        rc, o, e = self.issue(org=ORG)
        self.assertEqual(rc, 3, o + e)
        self.assertEqual(self.state.requests, [])
        self.assertIn(f"{self.base}/settings/{ORG}/auth-tokens/", e)
        self.assertIn("install --preset releases --vault example-site/prod/SENTRY_RELEASES_TOKEN", e)
        self.assertIn("a personal user auth token", e)
        self.assertIsNone(slot_value(NARROW_SLOT))
        self.assertFalse(self.m.LEDGER.exists())
        self.assertTrue(no_value(o + e))

    def test_the_read_preset_is_always_a_persons_step(self):
        self.oauth_admin()
        rc, o, e = self.issue(preset_key="read", target="/".join(READ_SLOT), org=ORG)
        self.assertEqual(rc, 3, o + e)
        self.assertEqual(self.state.requests, [])
        self.assertIn("developer-settings/new-internal/", e)
        self.assertIn("Issue & Event: Read", e)

    def test_an_oauth_token_without_org_tokens_mints_nothing(self):
        self.oauth_admin(("org:read", "org:write"))
        rc, o, e = self.issue()
        self.assertEqual(rc, 3, o + e)
        self.assertIn("org:tokens", e)
        self.assertFalse([r for r in self.state.requests if r[1] == "POST"])

    def test_a_broader_token_is_deleted_and_never_delivered(self):
        self.oauth_admin()
        self.state.members_open = True
        rc, o, e = self.issue()
        self.assertEqual(rc, 1, o + e)
        self.assertIn("broader than declared", e)
        self.assertIn("token 1 deleted", e)
        self.assertFalse(self.state.tokens[next(v for v, t in self.state.tokens.items() if t.get("id") == "1")]["alive"])
        self.assertIsNone(slot_value(NARROW_SLOT))
        self.assertFalse(self.m.LEDGER.exists())
        self.assertTrue(no_value(o + e))

    def test_a_failed_purpose_read_deletes_the_new_token(self):
        self.oauth_admin()
        self.state.refuse[f"/api/0/projects/{ORG}/{SITE}/releases/"] = 403
        rc, o, e = self.issue()
        self.assertEqual(rc, 1, o + e)
        self.assertIn("cannot do its purpose read", e)
        self.assertIn("token 1 deleted", e)
        self.assertIsNone(slot_value(NARROW_SLOT))
        self.assertTrue(no_value(o + e), "the provider's echoing body is withheld")

    def test_issue_again_retires_the_slots_previous_token(self):
        self.oauth_admin()
        self.assertEqual(self.issue()[0], 0)
        first = slot_value(NARROW_SLOT)
        rc, o, e = self.issue()
        self.assertEqual(rc, 0, o + e)
        self.assertIn("previous token: token 1 deleted", o)
        self.assertFalse(self.state.tokens[first]["alive"])
        self.assertNotEqual(slot_value(NARROW_SLOT), first)

    def test_replacing_a_slot_never_leaves_a_token_live_and_unrecorded(self):
        self.oauth_admin()
        self.assertEqual(self.issue()[0], 0)
        rc, o, e = run(self.m.cmd_install, "releases", "/".join(NARROW_SLOT), None, None, self.base, None, None,
                       wait=0)
        self.assertEqual(rc, 1, o + e)
        self.assertIn("revoke example-site/prod/SENTRY_RELEASES_TOKEN", e)
        self.assertEqual(json.loads(self.m.LEDGER.read_text())["issued"][
            "example-site/prod/SENTRY_RELEASES_TOKEN"]["minted_by"], "door")
        put(READ_SLOT, self.state.add(fake("sntryi_", "made-by-hand"), "integration",
                                      ("org:read", "project:read", "event:read")))
        self.assertEqual(run(self.m.cmd_install, "read", "/".join(READ_SLOT), ORG, None, self.base, None, None,
                             wait=0)[0], 0)
        rc, o, e = self.issue(preset_key="releases", target="/".join(READ_SLOT))
        self.assertEqual(rc, 0, o + e)
        self.assertIn("made by a person and stays live at Sentry", o)

    def test_a_dead_token_is_named_at_once_and_a_fresh_one_is_waited_for(self):
        put(NARROW_SLOT, self.state.add(fake_org_token(44, self.state.control, self.state.region), "org", ("org:ci",)))
        self.assertEqual(run(self.m.cmd_install, "releases", "/".join(NARROW_SLOT), None, None, self.base, None, None,
                             wait=0)[0], 0)
        self.state.tokens[slot_value(NARROW_SLOT)]["alive"] = False
        before = len(self.state.requests)
        rc, o, e = run(self.m.cmd_status, None, wait=0)
        self.assertEqual(rc, 1, o + e)
        self.assertEqual(len(self.state.requests) - before, 1, "a recorded token is asked once")
        # A fresh token that the region refuses for a moment is asked again (replication).
        self.oauth_admin()
        calls = {"n": 0}
        real = self.m._call

        def flaky(method, url, token, payload=None):
            if "/releases/" in url and calls["n"] < 2:
                calls["n"] += 1
                raise self.m.SentryError(401, "Sentry answered HTTP 401 on GET /api/0/projects/<id>/<id>/releases/")
            return real(method, url, token, payload)
        self.m._call = flaky
        rc, o, e = self.issue(target="/".join(READ_SLOT))
        self.assertEqual(rc, 0, o + e)
        self.assertEqual(calls["n"], 2)

    # ── install ────────────────────────────────────────────────────────────────

    def test_install_verifies_a_person_made_org_token_from_its_own_payload(self):
        value = self.state.add(fake_org_token(40, self.state.control, self.state.region), "org", ("org:ci",))
        put(NARROW_SLOT, value)
        rc, o, e = run(self.m.cmd_install, "releases", "/".join(NARROW_SLOT), None, None, self.base, None, None,
                       wait=0)
        self.assertEqual(rc, 0, o + e)
        row = json.loads(self.m.LEDGER.read_text())["issued"]["example-site/prod/SENTRY_RELEASES_TOKEN"]
        self.assertEqual((row["org"], row["minted_by"], row["token_id"], row["region_url"]),
                         (ORG, "installed", None, self.state.region))
        self.assertFalse([r for r in self.state.requests if r[2] == f"/api/0/organizations/{ORG}/"],
                         "the region came from the token's payload")
        self.assertTrue(no_value(self.surfaces(o + e)))

    def test_install_refuses_a_personal_token_and_a_wider_integration(self):
        put(NARROW_SLOT, self.state.add(fake("sntryu_", "person"), "personal", ("org:read",)))
        rc, o, e = run(self.m.cmd_install, "releases", "/".join(NARROW_SLOT), ORG, None, self.base, None, None, wait=0)
        self.assertEqual(rc, 3, o + e)
        self.assertIn("auth-tokens/", e)
        put(READ_SLOT, self.state.add(fake("sntryi_", "wide"), "integration",
                                      ("org:read", "project:read", "event:read", "member:read")))
        rc, o, e = run(self.m.cmd_install, "read", "/".join(READ_SLOT), ORG, SITE, self.base, None, None, wait=0)
        self.assertEqual(rc, 1, o + e)
        self.assertIn("preset read is exactly event:read, org:read, project:read", e)
        self.assertFalse(self.m.LEDGER.exists())

    def test_install_read_integration_reads_issues_and_not_members(self):
        put(READ_SLOT, self.state.add(fake("sntryi_", "triage"), "integration",
                                      ("org:read", "project:read", "event:read")))
        rc, o, e = run(self.m.cmd_install, "read", "/".join(READ_SLOT), ORG, SITE, self.base, None, None, wait=0)
        self.assertEqual(rc, 0, o + e)
        row = json.loads(self.m.LEDGER.read_text())["issued"]["example-site/prod/SENTRY_READ_TOKEN"]
        self.assertEqual(row["verified"]["proved"], ["/api/0/projects/<id>/<id>/issues/"])
        self.assertEqual(row["verified"]["refused"], ["/api/0/organizations/<id>/members/"])
        self.assertEqual(row["region_url"], self.state.region, "read from the organization's links")

    # ── revoke ─────────────────────────────────────────────────────────────────

    def test_the_broad_token_is_retired_only_after_a_narrow_one_verifies(self):
        admin = self.personal_admin()
        rc, o, e = run(self.m.cmd_revoke, None, "/".join(ADMIN_SLOT), ORG, self.base, wait=0)
        self.assertEqual(rc, 1, o + e)
        self.assertIn("no narrow token is recorded", e)
        self.assertEqual(slot_value(ADMIN_SLOT), admin)
        put(NARROW_SLOT, self.state.add(fake_org_token(41, self.state.control, self.state.region), "org", ("org:ci",)))
        self.assertEqual(run(self.m.cmd_install, "releases", "/".join(NARROW_SLOT), None, None, self.base, None, None,
                             wait=0)[0], 0)
        rc, o, e = run(self.m.cmd_revoke, None, "/".join(ADMIN_SLOT), ORG, self.base, wait=0)
        self.assertEqual(rc, 3, o + e)
        self.assertIn(f"{self.base}/settings/account/api/auth-tokens/", e)
        self.assertEqual(slot_value(ADMIN_SLOT), admin, "alive: kept")
        broad = json.loads(self.m.LEDGER.read_text())["broad"]["example-site/prod/SENTRY_ADMIN_TOKEN"]
        self.assertEqual(broad["state"], "revocation-requested")
        self.state.tokens[admin]["alive"] = False            # the person deleted it in Sentry
        rc, o, e = run(self.m.cmd_revoke, None, "/".join(ADMIN_SLOT), ORG, self.base, wait=0)
        self.assertEqual(rc, 0, o + e)
        self.assertIsNone(slot_value(ADMIN_SLOT))
        broad = json.loads(self.m.LEDGER.read_text())["broad"]["example-site/prod/SENTRY_ADMIN_TOKEN"]
        self.assertEqual((broad["state"], broad["slot"]), ("revoked-verified", "removed"))
        self.assertTrue(no_value(self.surfaces(o + e)))

    def test_a_narrow_token_that_no_longer_verifies_keeps_the_broad_one(self):
        admin = self.personal_admin()
        narrow = self.state.add(fake_org_token(42, self.state.control, self.state.region), "org", ("org:ci",))
        put(NARROW_SLOT, narrow)
        self.assertEqual(run(self.m.cmd_install, "releases", "/".join(NARROW_SLOT), None, None, self.base, None, None,
                             wait=0)[0], 0)
        self.state.tokens[narrow]["alive"] = False
        self.state.tokens[admin]["alive"] = False
        rc, o, e = run(self.m.cmd_revoke, None, "/".join(ADMIN_SLOT), ORG, self.base, wait=0)
        self.assertEqual(rc, 1, o + e)
        self.assertIn("no narrow token verified now", e)
        self.assertEqual(slot_value(ADMIN_SLOT), admin)

    def test_revoke_deletes_an_issued_token_proves_it_dead_and_removes_the_slot(self):
        self.oauth_admin()
        self.assertEqual(self.issue()[0], 0)
        narrow = slot_value(NARROW_SLOT)
        rc, o, e = run(self.m.cmd_revoke, "/".join(NARROW_SLOT), None, None, None, wait=0)
        self.assertEqual(rc, 0, o + e)
        self.assertFalse(self.state.tokens[narrow]["alive"])
        self.assertIsNone(slot_value(NARROW_SLOT))
        self.assertEqual(json.loads(self.m.LEDGER.read_text())["issued"], {})
        self.assertIn("Sentry now refuses it (401)", o)

    def test_an_installed_token_still_alive_is_a_persons_step(self):
        put(NARROW_SLOT, self.state.add(fake_org_token(43, self.state.control, self.state.region), "org", ("org:ci",)))
        run(self.m.cmd_install, "releases", "/".join(NARROW_SLOT), None, None, self.base, None, None, wait=0)
        rc, o, e = run(self.m.cmd_revoke, "/".join(NARROW_SLOT), None, None, None, wait=0)
        self.assertEqual(rc, 3, o + e)
        self.assertIn("cannot delete it by API", e)
        self.assertIsNotNone(slot_value(NARROW_SLOT))

    # ── list and status ────────────────────────────────────────────────────────

    def test_list_and_status_report_without_values(self):
        self.oauth_admin()
        self.assertEqual(self.issue()[0], 0)
        rc, o, e = run(self.m.cmd_list)
        self.assertEqual(rc, 0)
        self.assertIn("example-site/prod/SENTRY_RELEASES_TOKEN", o)
        self.assertIn("org:ci", o)
        rc, o, e = run(self.m.cmd_status, None, wait=0)
        self.assertEqual(rc, 0, o + e)
        self.assertIn("verified", o)
        self.state.tokens[slot_value(NARROW_SLOT)]["alive"] = False
        rc, o, e = run(self.m.cmd_status, None, wait=0)
        self.assertEqual(rc, 1, o + e)
        self.assertTrue(no_value(o + e))

    # ── hosts and errors ───────────────────────────────────────────────────────

    def test_hosts_are_https_or_loopback_and_regions_stay_under_the_base(self):
        m = self.m
        self.assertEqual(m.origin("https://sentry.io"), "https://sentry.io")
        self.assertEqual(m.origin("https://sentry.example.com/"), "https://sentry.example.com")
        for bad in ("http://sentry.example.com", "https://sentry.io/api/0/", "https://user@sentry.io",
                    "ftp://sentry.io", "https://sentry.io?x=1"):
            with self.assertRaises(m.Refusal, msg=bad):
                m.origin(bad)
        m.KNOWN_REGIONS = {"https://sentry.io": ("https://us.sentry.io", "https://de.sentry.io")}
        self.assertEqual(m.region_ok("https://de.sentry.io", "https://sentry.io"), "https://de.sentry.io")
        self.assertEqual(m.region_ok("https://sentry.example.com", "https://sentry.example.com"),
                         "https://sentry.example.com")
        for bad in ("https://sentry.io.evil.example", "https://evil.example", "http://us.sentry.io",
                    "https://eu.sentry.io", "https://other.sentry.io"):
            with self.assertRaises(m.Refusal, msg=bad):
                m.region_ok(bad, "https://sentry.io")
        self.assertEqual(m.choose_region("unused", "https://sentry.io", ORG, "de", None), "https://de.sentry.io")
        with self.assertRaises(m.Refusal):
            m.choose_region("unused", "https://sentry.example.com", ORG, "us", None)
        token = fake_org_token(50, "https://sentry.io", "https://us.sentry.io")
        self.assertEqual(m.org_payload(token)["org"], ORG)
        with self.assertRaises(m.Refusal):
            m.check_base(token, "https://sentry.example.com")
        self.assertEqual(m.kind_of(fake("sntryu_", "k")), "personal")
        self.assertEqual(m.kind_of("legacytokenwithoutprefix"), "unprefixed")

    def test_errors_carry_status_and_endpoint_shape_only(self):
        value = self.state.add(fake("sntrya_", "echo"), "oauth-app", ("org:read",))
        self.state.refuse["/api/0/"] = 500
        with self.assertRaises(self.m.SentryError) as ctx:
            self.m._call("GET", f"{self.base}/api/0/organizations/{ORG}/", value)
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("/api/0/organizations/<id>/", str(ctx.exception))
        self.assertTrue(no_value(str(ctx.exception)))
        with self.assertRaises(self.m.SentryError):
            self.m._call("GET", f"{self.base}/api/0/", "two words")
        self.assertEqual(len(self.state.requests), 1, "a malformed value is never sent")

    # ── review of #401: the findings, each with the case that failed before its fix ─────────────────

    def narrow_installed(self, n=60):
        put(NARROW_SLOT, self.state.add(fake_org_token(n, self.state.control, self.state.region), "org", ("org:ci",)))
        self.assertEqual(run(self.m.cmd_install, "releases", "/".join(NARROW_SLOT), None, None, self.base, None, None,
                             wait=0)[0], 0)

    def test_revoke_from_never_sends_or_removes_a_value_that_is_not_a_sentry_token(self):
        other = fake("ghp_", "another-service")          # finding 1, repro A
        put(ADMIN_SLOT, other)
        self.narrow_installed(61)
        rc, o, e = run(self.m.cmd_revoke, None, "/".join(ADMIN_SLOT), ORG, self.base, wait=0)
        self.assertEqual(rc, 1, o + e)
        self.assertIn("does not hold a Sentry token", e)
        self.assertFalse([r for r in self.state.requests if r[3] == other], "the value went nowhere")
        self.assertEqual(slot_value(ADMIN_SLOT), other)
        self.assertNotIn("example-site/prod/SENTRY_ADMIN_TOKEN", json.loads(self.m.LEDGER.read_text())["broad"])

    def test_revoke_from_needs_the_tokens_own_sentry_and_sends_nowhere_else(self):
        self.personal_admin()
        self.narrow_installed(62)
        before = len(self.state.requests)
        rc, o, e = run(self.m.cmd_revoke, None, "/".join(ADMIN_SLOT), ORG, None, wait=0)
        self.assertEqual(rc, 2, o + e)
        self.assertIn("--base-url", e)
        self.assertEqual(len(self.state.requests), before, "nothing was sent")
        org_token = fake_org_token(63, "https://sentry.example.com", "https://sentry.example.com")
        put(ADMIN_SLOT, org_token)
        rc, o, e = run(self.m.cmd_revoke, None, "/".join(ADMIN_SLOT), ORG, self.base, wait=0)
        self.assertEqual(rc, 2, o + e)
        self.assertIn("belongs to https://sentry.example.com", e)
        self.assertFalse([r for r in self.state.requests if r[3] == org_token])

    def test_a_401_counts_as_revoked_only_after_the_same_sentry_accepted_the_token(self):
        stranger = fake("sntryu_", "another-sentrys-token")   # Sentry here never knew it: 401 at once
        put(ADMIN_SLOT, stranger)
        self.narrow_installed(64)
        rc, o, e = run(self.m.cmd_revoke, None, "/".join(ADMIN_SLOT), ORG, self.base, wait=0)
        self.assertEqual(rc, 1, o + e)
        self.assertIn("does not prove it was revoked", e)
        self.assertEqual(slot_value(ADMIN_SLOT), stranger)
        self.assertEqual(json.loads(self.m.LEDGER.read_text())["broad"]["example-site/prod/SENTRY_ADMIN_TOKEN"]
                         ["state"], "unproven")

    def test_issue_records_the_minting_tokens_sentry_so_revoke_from_needs_no_base(self):
        admin = self.oauth_admin()
        self.assertEqual(self.issue()[0], 0)
        broad = json.loads(self.m.LEDGER.read_text())["broad"]["example-site/prod/SENTRY_ADMIN_TOKEN"]
        self.assertEqual((broad["base_url"], broad["kind"]), (self.base, "oauth-app"))
        self.assertTrue(broad["proved_on"])
        self.state.tokens[admin]["alive"] = False
        rc, o, e = run(self.m.cmd_revoke, None, "/".join(ADMIN_SLOT), ORG, None, wait=0)
        self.assertEqual(rc, 0, o + e)
        self.assertIsNone(slot_value(ADMIN_SLOT))

    def test_a_redirect_is_never_followed_with_the_token(self):
        got = []                                          # finding 2, repro B

        class Sink(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                got.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        threading.Thread(target=sink.serve_forever, daemon=True).start()

        class Hop(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{sink.server_address[1]}/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()
        hop = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hop)
        threading.Thread(target=hop.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(self.m.SentryError) as ctx:
                self.m._call("GET", f"http://127.0.0.1:{hop.server_address[1]}/api/0/", fake("sntryu_", "redirect"))
        finally:
            hop.shutdown()
            sink.shutdown()
            hop.server_close()
            sink.server_close()
        self.assertEqual(got, [], "the other host received nothing")
        self.assertEqual(ctx.exception.status, 302)
        self.assertIn("redirecting to localhost", str(ctx.exception))
        self.assertNotIn("/elsewhere", str(ctx.exception))

    def test_a_region_host_is_never_guessed_from_a_suffix(self):
        m = self.m                                        # finding 3, repro C
        for base, region in (("https://sentry.example.co.uk", "https://attacker.co.uk"),
                             ("https://sentry.corp.com.au", "https://evil.com.au"),
                             ("https://sentry.example.com", "https://region.example.com")):
            with self.assertRaises(m.Refusal, msg=region):
                m.region_ok(region, base)
        self.assertEqual(m.region_ok("https://region.example.com", "https://sentry.example.com", named=True),
                         "https://region.example.com")
        self.assertEqual(m.choose_region("unused", "https://sentry.example.com", ORG, None,
                                         "https://region.example.com"), "https://region.example.com")
        # A region a server names that is neither the base nor a documented one is refused, not used.
        m.KNOWN_REGIONS = {}
        value = self.state.add(fake("sntryi_", "region-from-server"), "integration", ("org:read",))
        with self.assertRaises(m.Refusal):
            m.choose_region(value, self.base, ORG, None, None)

    def test_a_failed_vault_put_deletes_the_minted_token_and_says_so(self):
        self.oauth_admin()                                # finding 4, repro D
        real = self.m.subprocess.run

        def boom(argv, **kw):
            if "put" in argv:
                raise __import__("subprocess").TimeoutExpired(argv, 60)
            return real(argv, **kw)
        self.m.subprocess = type("S", (), {"run": staticmethod(boom)})
        rc, o, e = self.issue()
        self.assertEqual(rc, 1, o + e)
        minted = [t for t in self.state.tokens.values() if t["kind"] == "org"]
        self.assertEqual(len(minted), 1)
        self.assertFalse(minted[0]["alive"], "the minted token was deleted")
        self.assertIn("token 1 deleted", e)
        self.assertNotIn("example-site/prod/SENTRY_RELEASES_TOKEN", json.loads(
            self.m.LEDGER.read_text())["issued"] if self.m.LEDGER.exists() else {})
        self.assertTrue(no_value(o + e))

    def test_a_failed_ledger_write_after_delivery_deletes_the_token_and_journals_its_id(self):
        self.oauth_admin()
        real_save = self.m.save_ledger

        def full_disk(doc):
            if doc["issued"]:
                raise OSError(28, "No space left on device")
            return real_save(doc)
        self.m.save_ledger = full_disk
        rc, o, e = self.issue()
        self.assertEqual(rc, 1, o + e)
        self.assertFalse(next(t for t in self.state.tokens.values() if t["kind"] == "org")["alive"])
        self.assertIn("now holds that token's value", e)
        rows = [json.loads(line) for line in vault.MOVES.read_text().splitlines() if line.strip()]
        failed = [r for r in rows if r.get("event") == "issue-failed"]
        self.assertTrue(failed and failed[-1].get("token") == "1", failed[-1:] if failed else rows[-3:])
        self.assertTrue(no_value(self.surfaces(o + e)))

    def test_main_turns_an_unexpected_error_into_a_clean_exit_without_its_message(self):
        def broken():
            raise ValueError("quoted " + fake("sntryu_", "in-an-error"))
        self.m.cmd_list = broken
        rc, o, e = run(self.m.main, ["list"])
        self.assertEqual(rc, 1)
        self.assertIn("unexpected ValueError", e)
        self.assertTrue(no_value(o + e))

    def test_minting_follows_sentrys_own_scope_rule(self):
        self.oauth_admin(("org:tokens", "org:admin"))     # finding 6: org:admin with org:tokens mints
        rc, o, e = self.issue()
        self.assertEqual(rc, 0, o + e)
        clear(NARROW_SLOT)
        self.m.LEDGER.unlink()
        self.oauth_admin(("org:tokens", "org:read"))      # mints; cannot delete, and says so
        rc, o, e = self.issue()
        self.assertEqual(rc, 0, o + e)
        self.assertIn("cannot delete an organization token", e)

    def test_what_org_ci_allows_is_stated_exactly(self):
        src = (ROOT / "tools" / "sentry.py").read_text(encoding="utf-8")   # finding 5
        self.assertNotIn("nothing else", src)
        for permission in ("OrganizationIntegrationsLoosePermission", "OrganizationCodeMappingsBulkPermission",
                           "OrganizationReleasePermission", "ProjectReleasePermission"):
            self.assertIn(permission, src)

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_the_ledger_is_owner_only(self):
        self.oauth_admin()
        self.assertEqual(self.issue()[0], 0)
        self.assertEqual(self.m.LEDGER.stat().st_mode & 0o777, 0o600)

    def test_the_door_source_keeps_the_rules(self):
        src = (ROOT / "tools" / "sentry.py").read_text(encoding="utf-8")
        self.assertIn("from None", src)
        self.assertNotIn("e.read()", src)
        self.assertNotIn('"--token"', src)
        self.assertIn("vault_only=True", src)
        self.assertTrue(all(p["scopes"] and "org:admin" not in p["scopes"] for p in self.m.PRESETS.values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
