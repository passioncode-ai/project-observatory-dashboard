#!/usr/bin/env python3
"""The DigitalOcean door's account half and its collector, against a fake API (OBS-155).

`tools/digitalocean.py` stashes one token per team, lists the teams, installs narrow
tokens minted in the control panel, rotates and revokes them; `collectors/scan_digitalocean.py`
reads what each team runs and costs. Everything runs against a loopback HTTP server that
plays DigitalOcean: no network, no real token. What is asserted:

1. no value reaches stdout, stderr, argv, a record or the raw scan;
2. a token is checked against the team it claims before anything is written;
3. `issue` refuses, because the public API cannot mint a token, and says how to `install`;
4. a revocation counts only when the provider then refuses the old value;
5. membership writes plan by default and write only with `--apply`;
6. the collector names every failure (401, 403, 429) and keeps what it could read,
   and never carries a database password.
"""
from __future__ import annotations

import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOOR = ROOT / "tools" / "digitalocean.py"
SCAN = ROOT / "collectors" / "scan_digitalocean.py"
FAILURES: list[str] = []

# Synthetic values only: shaped like a DigitalOcean token so the door's checks see one.
ADMIN_A = "dop_v1_" + "a1" * 32
ADMIN_B = "dop_v1_" + "b2" * 32
NARROW = "dop_v1_" + "c3" * 32
NARROW_NEW = "dop_v1_" + "d4" * 32
FOREIGN = "dop_v1_" + "e5" * 32
BROAD_A = "dop_v1_" + "a7" * 32          # team A, full access, but not the stashed one
READ2 = "dop_v1_" + "b8" * 32
ROT_A, ROT_B = "dop_v1_" + "c9" * 32, "dop_v1_" + "da" * 32
ROT_C, ROT_D = "dop_v1_" + "1c" * 32, "dop_v1_" + "2d" * 32
NEWLINED = "dop_v1_" + "eb" * 16 + "\n" + "fc" * 16    # http.client quotes such a header in its error
DB_SECRET = "db-secret-" + "f6" * 10
VALUES = (ADMIN_A, ADMIN_B, NARROW, NARROW_NEW, FOREIGN, BROAD_A, READ2, ROT_A, ROT_B, ROT_C, ROT_D, DB_SECRET,
          "eb" * 16, "fc" * 16)
#: What a narrow token of the fake cannot read: the probes outside its preset.
NARROW_FORBID = ["/v2/customers", "/v2/account/keys", "/v2/tags"]
TEAM_A = {"uuid": "11111111-1111-4111-8111-111111111111", "name": "Example Team"}
TEAM_B = {"uuid": "22222222-2222-4222-8222-222222222222", "name": "Other Team"}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f": {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def clean(text: str) -> bool:
    return not any(v in text for v in VALUES)


# ── the fake provider ──────────────────────────────────────────────────────────

class Provider:
    def __init__(self) -> None:
        self.tokens = {
            ADMIN_A: {"team": TEAM_A, "alive": True, "forbid": [], "org": True},
            ADMIN_B: {"team": TEAM_B, "alive": True, "forbid": [], "org": False},
            NARROW: {"team": TEAM_A, "alive": True, "forbid": list(NARROW_FORBID), "org": False},
            NARROW_NEW: {"team": TEAM_A, "alive": True, "forbid": list(NARROW_FORBID), "org": False},
            FOREIGN: {"team": TEAM_B, "alive": True, "forbid": [], "org": False},
            BROAD_A: {"team": TEAM_A, "alive": True, "forbid": [], "org": False},
            READ2: {"team": TEAM_A, "alive": True, "forbid": list(NARROW_FORBID), "org": False},
            ROT_A: {"team": TEAM_A, "alive": True, "forbid": list(NARROW_FORBID), "org": False},
            ROT_B: {"team": TEAM_A, "alive": True, "forbid": list(NARROW_FORBID), "org": False},
            ROT_C: {"team": TEAM_A, "alive": True, "forbid": list(NARROW_FORBID), "org": False},
            ROT_D: {"team": TEAM_A, "alive": True, "forbid": list(NARROW_FORBID), "org": False},
        }
        self.revocable = True           # does the OAuth revoke endpoint kill a PAT?
        self.rate_limited: set[str] = set()
        self.unavailable: set[str] = set()      # paths answering 503
        self.delay = 0.0                # seconds per request, to widen a race window
        self.calls: list[tuple[str, str, str]] = []   # (method, path, team uuid or "?")
        self.created_teams: list[dict] = []
        self.data = {
            TEAM_A["uuid"]: {
                "droplets": [
                    {"id": 101, "name": "web-1", "status": "active", "size_slug": "s-1vcpu-1gb",
                     "size": {"slug": "s-1vcpu-1gb", "price_monthly": 6.0}, "region": {"slug": "fra1"},
                     "created_at": "2026-01-02T00:00:00Z", "tags": [], "volume_ids": []},
                    {"id": 102, "name": "old-box", "status": "off", "size_slug": "s-2vcpu-4gb",
                     "size": {"slug": "s-2vcpu-4gb", "price_monthly": 24.0}, "region": {"slug": "ams3"},
                     "created_at": "2025-03-04T00:00:00Z", "tags": ["legacy"], "volume_ids": ["v-1"]},
                ],
                "apps": [
                    {"id": "app-1", "spec": {"name": "site", "services": [
                        {"name": "web", "github": {"repo": "example-org/example-site", "branch": "main"}}]},
                     "region": {"slug": "fra"}, "live_url": "https://site.example.invalid",
                     "active_deployment": {"id": "dep-1", "phase": "ACTIVE"},
                     "created_at": "2026-02-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z"},
                    {"id": "app-2", "spec": {"name": "broken", "workers": [
                        {"name": "w", "git": {"repo_clone_url": "https://github.com/example-org/broken.git"}}]},
                     "region": {"slug": "nyc"}, "created_at": "2026-03-01T00:00:00Z",
                     "in_progress_deployment": {"id": "dep-9", "phase": "ERROR"}},
                    {"id": "app-3", "spec": {"name": "odd", "services": [{"name": "w", "github": "not-an-object"}]}},
                ],
                "databases": [
                    {"id": "db-1", "name": "shop-db", "engine": "pg", "version": "16", "size": "db-s-1vcpu-1gb",
                     "num_nodes": 1, "region": "fra1", "status": "online", "created_at": "2025-01-01T00:00:00Z",
                     "connection": {"host": "shop-db.example", "port": 25060, "user": "doadmin", "password": DB_SECRET,
                                    "uri": f"postgresql://doadmin:{DB_SECRET}@shop-db.example:25060/defaultdb"},
                     "users": [{"name": "doadmin", "password": DB_SECRET}]},
                ],
                "volumes": [{"id": "v-1", "name": "data", "size_gigabytes": 100, "region": {"slug": "ams3"},
                             "droplet_ids": [102], "created_at": "2025-03-04T00:00:00Z"}],
                "domains": [{"name": "example.invalid", "ttl": 1800, "zone_file": "SOA ..."}],
                "balance": {"month_to_date_balance": "30.00", "account_balance": "0.00",
                            "month_to_date_usage": "30.00", "generated_at": "2026-10-19T00:00:00Z"},
            },
            TEAM_B["uuid"]: {"droplets": [], "apps": [], "databases": [], "volumes": [], "domains": [],
                             "balance": {"month_to_date_usage": "0.00", "account_balance": "0.00",
                                         "generated_at": "2026-10-19T00:00:00Z"}},
        }


def serve(provider: Provider) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):            # quiet
            pass

        def _send(self, code: int, body: dict | None = None, headers: dict | None = None) -> None:
            raw = json.dumps(body if body is not None else {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _token(self) -> str:
            return (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()

        def _handle(self, method: str) -> None:
            url = urllib.parse.urlsplit(self.path)
            path = url.path
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            if provider.delay:
                import time
                time.sleep(provider.delay)
            tok = provider.tokens.get(self._token())
            provider.calls.append((method, path, tok["team"]["uuid"] if tok else "?"))
            if path == "/v1/oauth/revoke":
                form = urllib.parse.parse_qs(body.decode())
                target = provider.tokens.get((form.get("token") or [""])[0])
                if target and provider.revocable:
                    target["alive"] = False
                return self._send(200, {})
            if not tok or not tok["alive"]:
                return self._send(401, {"id": "Unauthorized", "message": "Unable to authenticate you"})
            if any(path.startswith(p) for p in tok["forbid"]):
                return self._send(403, {"id": "Forbidden", "message": "You are not authorized"})
            if path in provider.unavailable:
                return self._send(503, {"id": "service_unavailable"})
            if path in provider.rate_limited:
                return self._send(429, {"id": "too_many_requests"}, {"Retry-After": "30"})
            team = tok["team"]
            data = provider.data[team["uuid"]]
            if path == "/v2/account":
                return self._send(200, {"account": {"uuid": "user-1", "email": "operator@example.invalid",
                                                    "status": "active", "team": dict(team)}})
            if path == "/v2/teams":
                return self._send(200, {"teams": [
                    {"id": 1, "uuid": TEAM_A["uuid"], "name": TEAM_A["name"], "email": "a@example.invalid",
                     "user_role": "owner"},
                    {"id": 2, "uuid": TEAM_B["uuid"], "name": TEAM_B["name"], "email": "b@example.invalid",
                     "user_role": "member"},
                    {"id": 3, "uuid": "33333333-3333-4333-8333-333333333333", "name": "Unstashed Team",
                     "email": "c@example.invalid", "user_role": "biller"}], "meta": {"total": 3}})
            if path == "/v2/organizations/teams":
                if not tok["org"]:
                    return self._send(412, {"id": "precondition_failed", "message": "not an organization"})
                return self._send(200, {"teams": [dict(TEAM_A, member_count=3, status="joined")]})
            if path == "/v2/organizations/team" and method == "POST":
                if not tok["org"]:
                    return self._send(412, {"id": "precondition_failed"})
                doc = json.loads(body or b"{}")
                provider.created_teams.append(doc)
                return self._send(201, {"team": {"uuid": "44444444-4444-4444-8444-444444444444",
                                                 "name": doc.get("name")},
                                        "invitations": [{"email": i["email"], "status": "ok"}
                                                        for i in doc.get("invitations") or []]})
            if path in ("/v2/account/keys", "/v2/tags"):
                return self._send(200, {path.rsplit("/", 1)[1]: [], "links": {}, "meta": {"total": 0}})
            if path == "/v2/customers/my/balance":
                return self._send(200, data["balance"])
            if path.startswith("/v2/domains/"):
                name = path.split("/")[3]
                if any(d["name"] == name for d in data["domains"]):
                    return self._send(200, {"domain": {"name": name}})
                return self._send(404, {"id": "not_found"})
            key = {"/v2/droplets": "droplets", "/v2/apps": "apps", "/v2/databases": "databases",
                   "/v2/volumes": "volumes", "/v2/domains": "domains"}.get(path)
            if key:
                rows = data[key]
                return self._send(200, {key: rows, "links": {}, "meta": {"total": len(rows)}})
            return self._send(404, {"id": "not_found"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def environment(tmp: pathlib.Path, base: str) -> dict:
    (tmp / "vault").mkdir(exist_ok=True)
    (tmp / "secrets").mkdir(exist_ok=True)
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp), "OBSERVATORY_HOME": str(tmp / "workspace"),
            "OBSERVATORY_SECRETS": str(tmp / "secrets"), "PYTHONDONTWRITEBYTECODE": "1",
            "OBSERVATORY_VAULT_DIR": str(tmp / "vault"), "OBSERVATORY_DO_API": base,
            "DOCTL_CONFIG": str(tmp / "doctl-config.yaml")}


def door(env: dict, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(DOOR), *args], env=env, input=stdin,
                          capture_output=True, text=True, timeout=120)


def all_files_clean(tmp: pathlib.Path) -> list[str]:
    """Every record, journal or scan under `tmp` that holds a value (value files have no suffix)."""
    bad = []
    for p in tmp.rglob("*"):
        if not p.is_file():
            continue
        if p.name.endswith(".meta.json") or p.suffix in (".json", ".jsonl", ".log"):
            if not clean(p.read_text(errors="ignore")):
                bad.append(str(p.relative_to(tmp)))
    return bad


# ── the checks ─────────────────────────────────────────────────────────────────

def case_stash_teams_and_members(tmp: pathlib.Path, env: dict, provider: Provider) -> None:
    r = door(env, "stash", stdin=ADMIN_A + "\n")
    out = r.stdout + r.stderr
    check("stash accepts a team token on stdin", r.returncode == 0 and "example-team" in r.stdout, out[-300:])
    check("stash prints no value and no email", clean(out) and "@example.invalid" not in out, out[-300:])
    stash = tmp / "secrets" / "digitalocean-admin" / "example-team"
    check("the team token is stored 0600", stash.is_file() and stash.stat().st_mode & 0o777 == 0o600)
    meta = json.loads((stash.parent / "example-team.meta.json").read_text())
    check("its record names the team, its uuid and the role, never the value",
          meta.get("team_uuid") == TEAM_A["uuid"] and meta.get("role") == "owner"
          and clean(json.dumps(meta)), json.dumps(meta)[:300])

    r = door(env, "stash", stdin=ADMIN_A + " " + ADMIN_B + "\n")
    check("two values on stdin are refused", r.returncode != 0 and clean(r.stdout + r.stderr))
    r = door(env, "stash", stdin="dop_v1_" + "9" * 64 + "\n")
    check("a token DigitalOcean refuses is not stashed",
          r.returncode != 0 and "401" in r.stderr and not (stash.parent / "unnamed").exists(), r.stderr[-200:])

    # BOOTSTRAP FROM doctl: the config is read directly, the value never printed.
    (tmp / "doctl-config.yaml").write_text(
        f"access-token: {FOREIGN}\nauth-contexts:\n  default: \"\"\n  work: '{ADMIN_B}'\ncontext: work\noutput: text\n")
    r = door(env, "stash", "--from-doctl", "work")
    out = r.stdout + r.stderr
    check("stash --from-doctl imports a named context without printing it",
          r.returncode == 0 and "other-team" in r.stdout and clean(out), out[-300:])
    meta_b = json.loads((stash.parent / "other-team.meta.json").read_text())
    check("its record says it came from doctl, by context name", meta_b.get("source") == "doctl:work", str(meta_b))
    real = tmp / "doctl-config.yaml"
    link = tmp / "doctl-link.yaml"
    link.symlink_to(real)
    r = door(dict(env, DOCTL_CONFIG=str(link)), "stash", "--from-doctl", "work")
    check("a symlinked doctl config is refused", r.returncode != 0 and "symbolic link" in r.stderr,
          r.stderr[-200:])
    link.unlink()
    r = door(env, "stash", "--from-doctl", "nosuch")
    check("an unknown doctl context is refused by name, listing the known ones",
          r.returncode != 0 and "work" in r.stderr and clean(r.stderr), r.stderr[-200:])

    r = door(env, "teams")
    out = r.stdout + r.stderr
    check("teams lists every stashed team with its role",
          r.returncode == 0 and "example-team" in r.stdout and "owner" in r.stdout
          and "other-team" in r.stdout and "member" in r.stdout, out[-500:])
    check("teams names a team the user belongs to that has no stash here",
          "Unstashed Team" in r.stdout and "no token stashed" in r.stdout, out[-500:])
    check("teams prints no value", clean(out))

    r = door(env, "members", "--team", "example-team")
    out = r.stdout + r.stderr
    check("members reports the role and the organisation's member count",
          r.returncode == 0 and "owner" in r.stdout and "3 member(s)" in r.stdout, out[-400:])
    check("members says the public API lists no members, with the reference",
          "public API" in r.stdout and "docs.digitalocean.com" in r.stdout, out[-400:])
    r = door(env, "members", "--team", "other-team")
    check("members outside an organisation names the precondition rather than failing",
          r.returncode == 0 and "organization" in r.stdout, (r.stdout + r.stderr)[-300:])
    r = door(env, "members", "--team", "nosuch")
    check("members of an unknown team is refused", r.returncode != 0)


def case_team_create(env: dict, provider: Provider) -> None:
    r = door(env, "team", "create", "staging", "--org", "example-team",
             "--invite", "dev@example.invalid:member", "--invite", "money@example.invalid:biller")
    check("team create plans by default and writes nothing",
          r.returncode == 0 and "dry run" in r.stdout and not provider.created_teams, (r.stdout + r.stderr)[-300:])
    check("the plan masks the invited addresses", "dev@example.invalid" not in r.stdout and "@example.invalid" in r.stdout)
    r = door(env, "team", "create", "staging", "--org", "example-team", "--invite", "x@example.invalid:owner")
    check("the owner role cannot be invited", r.returncode != 0 and "owner" in r.stderr)
    r = door(env, "team", "create", "staging", "--org", "example-team",
             "--invite", "dev@example.invalid:member", "--apply")
    check("team create --apply writes once, with the invitations",
          r.returncode == 0 and len(provider.created_teams) == 1
          and provider.created_teams[0]["invitations"] == [{"email": "dev@example.invalid", "role": "member"}],
          (r.stdout + r.stderr)[-300:])
    r = door(env, "team", "create", "x", "--org", "other-team", "--apply")
    check("team create outside an organisation is refused with the precondition",
          r.returncode != 0 and "organization" in r.stderr and len(provider.created_teams) == 1, r.stderr[-200:])


def case_issue_install_rotate_revoke(tmp: pathlib.Path, env: dict, provider: Provider) -> None:
    r = door(env, "presets")
    check("presets list the scopes to tick in the control panel",
          r.returncode == 0 and "droplet:read" in r.stdout and "billing:read" in r.stdout)
    r = door(env, "issue", "--preset", "read-only", "--team", "example-team", "--vault", "shop/prod/DO_TOKEN")
    check("issue refuses: the public API cannot mint a token",
          r.returncode == 3 and "cannot create" in r.stderr and "install" in r.stderr, r.stderr[-400:])

    r = door(env, "install", "--preset", "read-only", "--team", "example-team", "--vault", "shop/prod/DO_TOKEN",
             stdin=FOREIGN + "\n")
    check("install refuses a token of another team", r.returncode != 0 and "team" in r.stderr
          and not (tmp / "vault" / "shop" / "prod" / "DO_TOKEN").exists(), r.stderr[-300:])
    r = door(env, "install", "--preset", "billing-read", "--team", "example-team",
             "--vault", "shop/prod/DO_BILLING", stdin=NARROW + "\n")
    check("install refuses a token that cannot do its preset's read",
          r.returncode != 0 and "/v2/customers/my/balance" in r.stderr
          and not (tmp / "vault" / "shop" / "prod" / "DO_BILLING").exists(), r.stderr[-300:])
    r = door(env, "install", "--preset", "read-only", "--team", "example-team", "--vault", "shop/prod/DO_TOKEN",
             "--project", "project:shop", stdin=NARROW + "\n")
    out = r.stdout + r.stderr
    slot = tmp / "vault" / "shop" / "prod" / "DO_TOKEN"
    check("install verifies and delivers a narrow token to the vault",
          r.returncode == 0 and slot.is_file() and slot.read_text().strip() == NARROW, out[-400:])
    check("install prints no value", clean(out))
    rec_dir = tmp / "secrets" / "digitalocean-issued"
    recs = sorted(rec_dir.glob("*.meta.json"))
    rec = json.loads(recs[0].read_text()) if recs else {}
    check("install leaves a record: team, preset, slot, declared scopes, never the value",
          rec.get("team_uuid") == TEAM_A["uuid"] and rec.get("preset") == "read-only"
          and rec.get("vault") == "shop/prod/DO_TOKEN" and "droplet:read" in rec.get("scopes_declared", [])
          and clean(json.dumps(rec)), json.dumps(rec)[:300])
    label = recs[0].name.removesuffix(".meta.json") if recs else "?"

    r = door(env, "install", "--preset", "observatory-read", "--team", "example-team", stdin=NARROW + "\n")
    read_file = tmp / "secrets" / "digitalocean-read.d" / "example-team"
    check("observatory-read goes to the collector's folder, not the vault",
          r.returncode == 0 and read_file.is_file() and read_file.stat().st_mode & 0o777 == 0o600,
          (r.stdout + r.stderr)[-300:])

    r = door(env, "list")
    check("list shows stashes and installed tokens without values",
          r.returncode == 0 and label in r.stdout and "example-team" in r.stdout and clean(r.stdout + r.stderr))
    r = door(env, "teams")
    check("teams names the vault slots that belong to each team",
          "shop/prod/DO_TOKEN" in r.stdout, r.stdout[-500:])
    r = door(env, "ping")
    check("ping finds every token alive", r.returncode == 0 and "DEAD" not in r.stdout + r.stderr,
          (r.stdout + r.stderr)[-400:])

    # ROTATE: a new value in, the old one revoked and PROVED dead.
    r = door(env, "rotate", label, stdin=NARROW_NEW + "\n")
    out = r.stdout + r.stderr
    check("rotate puts the new value in the slot", r.returncode == 0 and slot.read_text().strip() == NARROW_NEW,
          out[-400:])
    check("rotate revokes the old value and proves it dead", not provider.tokens[NARROW]["alive"]
          and "revoked (DigitalOcean now refuses it)" in r.stdout, out[-400:])
    check("rotate prints no value", clean(out))
    rec = json.loads(recs[0].read_text())
    check("the record counts the rotation", rec.get("rotations") == 1 and rec.get("rotated_on"), str(rec)[:300])

    # A provider that does not honour the revoke for a personal token: the door says so.
    provider.revocable = False
    r = door(env, "rotate", label, stdin=NARROW + "\n")   # NARROW is dead: refused before anything changes
    check("rotate refuses a value DigitalOcean does not accept", r.returncode != 0
          and slot.read_text().strip() == NARROW_NEW, (r.stdout + r.stderr)[-300:])
    r = door(env, "revoke", label)
    out = r.stdout + r.stderr
    check("revoke that DigitalOcean does not honour keeps everything and sends the operator to the panel",
          r.returncode != 0 and slot.is_file() and "cloud.digitalocean.com" in out and clean(out), out[-400:])
    provider.revocable = True
    r = door(env, "revoke", label)
    check("revoke, once proved, removes the slot and the record",
          r.returncode == 0 and not slot.exists() and not recs[0].exists()
          and not provider.tokens[NARROW_NEW]["alive"], (r.stdout + r.stderr)[-300:])
    check("no meta, journal or scan file under the sandbox holds a value",
          not all_files_clean(tmp), str(all_files_clean(tmp)))


def case_review_round_1(tmp: pathlib.Path, env: dict, provider: Provider) -> None:
    """The first adversarial round on #306: each HIGH and MEDIUM, as the operator would hit it."""
    vault = tmp / "vault"
    # HIGH 2: the team's own token, or any token wider than its preset, is never installed as narrow.
    r = door(env, "install", "--preset", "read-only", "--team", "example-team", "--vault", "r1/prod/DO_TOKEN",
             stdin=ADMIN_A + "\n")
    check("install refuses the team's own stashed token", r.returncode != 0 and "stashed token" in r.stderr
          and not (vault / "r1").exists() and clean(r.stdout + r.stderr), r.stderr[-300:])
    r = door(env, "install", "--preset", "read-only", "--team", "example-team", "--vault", "r1/prod/DO_TOKEN",
             stdin=BROAD_A + "\n")
    check("install refuses a token broader than its preset", r.returncode != 0 and "broader" in r.stderr
          and "/v2/account/keys" in r.stderr and not (vault / "r1").exists(), r.stderr[-300:])

    # MEDIUM 4: installing over a live label would orphan the old token.
    r = door(env, "install", "--preset", "read-only", "--team", "example-team", "--vault", "r1/prod/DO_TOKEN",
             stdin=ROT_A + "\n")
    check("a narrow token installs", r.returncode == 0, (r.stdout + r.stderr)[-300:])
    label = "read-only--r1-prod-do_token"
    r = door(env, "install", "--preset", "read-only", "--team", "example-team", "--vault", "r1/prod/DO_TOKEN",
             stdin=ROT_B + "\n")
    slot = vault / "r1" / "prod" / "DO_TOKEN"
    check("install over an installed label is refused and names rotate",
          r.returncode != 0 and "rotate" in r.stderr and slot.read_text().strip() == ROT_A, r.stderr[-300:])

    # MEDIUM 5: two rotations at once are serialised: both counted, every replaced value revoked.
    provider.delay = 0.3       # each run spends seconds between its first read and its last write
    procs = [subprocess.Popen([sys.executable, str(DOOR), "rotate", label], env=env, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in (0, 1)]
    for p, v in zip(procs, (ROT_C, ROT_D)):
        p.stdin.write(v + "\n")
        p.stdin.close()
    outs = [(p.stdout.read(), p.stderr.read(), p.wait(timeout=120))[:2] for p in procs]
    provider.delay = 0.0
    rec = json.loads((tmp / "secrets" / "digitalocean-issued" / f"{label}.meta.json").read_text())
    held = slot.read_text().strip()
    alive = [v for v in (ROT_A, ROT_C, ROT_D) if provider.tokens[v]["alive"]]
    check("concurrent rotations both count, and only the held value stays alive",
          all(p.returncode == 0 for p in procs) and rec.get("rotations") == 2 and alive == [held],
          f"rotations={rec.get('rotations')} alive={len(alive)} codes={[p.returncode for p in procs]} "
          f"{[o[1][-200:] for o in outs]}")
    check("and neither prints a value", all(clean(o[0] + o[1]) for o in outs))
    check("the doors hold one records lock around install, rotate and revoke",
          DOOR.read_text().count("with records_lock():") == 3)

    # Confirming round: a probe that answers 429 or 503 proves nothing; a broad token is not let in.
    before = slot.read_text()
    for kind, bucket in (("429", provider.rate_limited), ("503", provider.unavailable)):
        bucket.update({"/v2/account/keys", "/v2/tags"})
        r = door(env, "rotate", label, stdin=BROAD_A + "\n")
        bucket.clear()
        check(f"a breadth probe answering {kind} refuses the rotate and keeps the installed value",
              r.returncode != 0 and "could not prove the token is narrow" in r.stderr and kind in r.stderr
              and slot.read_text() == before and provider.tokens[BROAD_A]["alive"]
              and clean(r.stdout + r.stderr), (r.stdout + r.stderr)[-300:])
    provider.rate_limited.update({"/v2/account/keys", "/v2/tags"})
    r = door(env, "install", "--preset", "observatory-read", "--team", "other-team", stdin=FOREIGN + "\n")
    provider.rate_limited.clear()
    check("and refuses the install, writing nothing",
          r.returncode != 0 and "could not prove" in r.stderr
          and not (tmp / "secrets" / "digitalocean-read.d" / "other-team").exists(), r.stderr[-300:])

    # HIGH 1: with its value gone, revoke claims nothing and keeps the record.
    slot.unlink()
    r = door(env, "revoke", label)
    rec_file = tmp / "secrets" / "digitalocean-issued" / f"{label}.meta.json"
    check("revoke with no value refuses, keeps the record and says it cannot prove anything",
          r.returncode != 0 and rec_file.is_file() and "cannot revoke" in r.stderr
          and "revoked" not in r.stdout, (r.stdout + r.stderr)[-300:])
    r = door(env, "revoke", label, "--forget")
    check("revoke --forget drops the record and says no revocation was observed",
          r.returncode == 0 and not rec_file.exists() and "did NOT observe" in r.stdout, r.stdout[-300:])

    # HIGH 3: a value with a newline never reaches a header, and nothing prints it.
    planted = vault / "r3" / "prod" / "DIGITALOCEAN_TOKEN"
    planted.parent.mkdir(parents=True, mode=0o700)
    planted.write_text(NEWLINED + "\n")
    planted.chmod(0o600)
    r = door(env, "teams", "--probe")
    check("teams --probe over a slot with a newline inside its value prints no value and no traceback",
          r.returncode == 0 and clean(r.stdout + r.stderr) and "Traceback" not in r.stderr
          and "r3/prod/DIGITALOCEAN_TOKEN" in r.stdout, (r.stdout + r.stderr)[-400:])
    rec_file.write_text(json.dumps({"kind": "installed", "label": label, "preset": "read-only",
                                    "team_label": "example-team", "team_uuid": TEAM_A["uuid"],
                                    "vault": "r3/prod/DIGITALOCEAN_TOKEN", "rotations": 0}))
    rec_file.chmod(0o600)
    for args, stdin in ((("ping",), None), (("revoke", label), None), (("rotate", label), READ2 + "\n")):
        r = door(env, *args, stdin=stdin)
        check(f"{args[0]} over that slot prints no value and no traceback",
              clean(r.stdout + r.stderr) and "Traceback" not in r.stderr, (r.stdout + r.stderr)[-300:])
        if args[0] in ("revoke", "rotate"):
            check(f"{args[0]} says the malformed old value could not be checked, not that DigitalOcean accepts it",
                  "malformed" in r.stderr and "STILL ACCEPTED" not in r.stderr and "still accepts" not in r.stderr
                  and "cloud.digitalocean.com" in r.stderr, r.stderr[-300:])
    r = door(env, "stash", stdin="dop_v1_" + "ab" * 8 + "\x07" + "cd" * 8 + "\n")
    check("a control character on stdin is refused before anything is sent",
          r.returncode != 0 and "cannot have" in r.stderr, r.stderr[-200:])
    rec_file.unlink()
    planted.unlink()
    provider.tokens[READ2]["alive"] = True


def case_api_override_is_loopback_only(env: dict) -> None:
    e = dict(env, OBSERVATORY_DO_API="https://evil.example.invalid")
    r = door(e, "teams")
    check("an API override that is not loopback is refused before any token is sent",
          r.returncode != 0 and "loopback" in r.stderr, r.stderr[-200:])


def case_collector(tmp: pathlib.Path, env: dict, provider: Provider) -> None:
    out_file = tmp / "digitalocean.json"
    r = subprocess.run([sys.executable, str(SCAN), str(out_file)], env=env, capture_output=True, text=True, timeout=60)
    check("the collector does nothing while the integration is off",
          r.returncode == 0 and "not configured" in r.stdout and not out_file.exists(), (r.stdout + r.stderr)[-300:])
    import re
    version = int(re.search(r"^CONFIG_VERSION\s*=\s*(\d+)", (ROOT / "configuration.py").read_text(), re.M).group(1))
    settings = tmp / "workspace" / "config" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"schema_version": version, "sources": {},
                                    "integrations": {"digitalocean": True}, "features": {}}))
    provider.rate_limited.add("/v2/volumes")
    r = subprocess.run([sys.executable, str(SCAN), str(out_file)], env=env,
                       capture_output=True, text=True, timeout=120)
    provider.rate_limited.clear()
    check("the collector runs", r.returncode == 0 and out_file.is_file(), (r.stdout + r.stderr)[-400:])
    doc = json.loads(out_file.read_text()) if out_file.is_file() else {}
    text = json.dumps(doc)
    check("the scan holds no token and no database password", clean(text) and "password" not in text
          and "connection" not in text, text[:200])
    teams = {t["label"]: t for t in doc.get("teams", [])}
    a = teams.get("example-team", {})
    check("it reads with the narrowest token it holds (observatory-read)", a.get("token_kind") == "observatory-read",
          str(a.get("token_kind")))
    check("droplets carry status and monthly price",
          any(d["name"] == "old-box" and d["status"] == "off" and d["price_monthly"] == 24.0
              for d in a.get("droplets", [])), str(a.get("droplets"))[:300])
    check("apps carry the repository they deploy from",
          any(x["name"] == "site" and x["repositories"] == ["example-org/example-site"] for x in a.get("apps", []))
          and any(x["name"] == "broken" and x["repositories"] == ["example-org/broken"] for x in a.get("apps", [])),
          str(a.get("apps"))[:400])
    check("databases keep size and status, drop connection and users",
          a.get("databases") and a["databases"][0]["name"] == "shop-db" and "users" not in a["databases"][0])
    reasons = {(d["source"], d["reason"].split(" ")[0]) for d in doc.get("degraded", [])}
    check("a 403 on balance is named for that resource, the rest still lands",
          any(s.endswith("example-team/balance") and "403" in d["reason"] for d in doc.get("degraded", [])
              for s in [d["source"]]) and a.get("droplets"), str(doc.get("degraded")))
    check("a 429 is named as a rate limit with its wait",
          any(d["source"].endswith("example-team/volumes") and "429" in d["reason"] and "30" in d["reason"]
              for d in doc.get("degraded", [])), str(reasons))
    check("one malformed app row is skipped and named; the other apps land",
          len(a.get("apps") or []) == 2 and any(d["source"].endswith("example-team/apps") and "1 row" in d["reason"]
                                                 for d in doc.get("degraded", [])), str(doc.get("degraded")))
    b = teams.get("other-team", {})
    check("a team read with its stashed token says so", b.get("token_kind") == "stash", str(b))

    import time as _time
    r = subprocess.run([sys.executable, str(SCAN), str(out_file)], capture_output=True, text=True, timeout=120,
                       env=dict(env, OBSERVATORY_STEP_DEADLINE=str(_time.time() + 5)))
    doc = json.loads(out_file.read_text())
    check("past the step's deadline the scan is still written, naming the teams it did not read",
          r.returncode == 0 and any("deadline" in d["reason"] for d in doc["degraded"]), str(doc["degraded"])[:300])
    provider.tokens[ADMIN_B]["alive"] = False
    r = subprocess.run([sys.executable, str(SCAN), str(out_file)], env=env,
                       capture_output=True, text=True, timeout=120)
    doc = json.loads(out_file.read_text())
    check("a dead token is a named 401 for its team, not an empty team",
          any(d["source"] == "digitalocean:other-team" and "401" in d["reason"] for d in doc["degraded"])
          and not any(t["label"] == "other-team" for t in doc["teams"]), str(doc["degraded"]))
    provider.tokens[ADMIN_B]["alive"] = True


def case_findings(provider: Provider) -> None:
    sys.path.insert(0, str(ROOT / "tools"))
    import digitalocean_findings as F
    from datetime import date
    doc = {"scanned_on": "2026-10-19", "teams": [{
        "label": "example-team", "team_name": "Example Team",
        "droplets": [{"id": 102, "name": "old-box", "status": "off", "price_monthly": 24.0, "size_slug": "s"},
                     {"id": 101, "name": "web-1", "status": "active", "price_monthly": 6.0, "size_slug": "s"}],
        "apps": [{"id": "app-1", "name": "site", "serving": True, "repositories": ["example-org/example-site"]},
                 {"id": "app-2", "name": "broken", "serving": False, "repositories": ["example-org/broken"],
                  "last_phase": "ERROR"}]}]}
    repos = [{"id": "repository:example-org/broken", "name_with_owner": "example-org/broken"}]
    relations = [{"type": "implemented_by", "from": "project:shop", "to": "repository:example-org/broken"}]
    rows = F.findings(doc, repos, relations, today=date(2026, 10, 20))
    types = sorted(r["type"] for r in rows)
    check("a powered-off Droplet is a billed finding", "digitalocean.droplet_off_billed" in types, str(types))
    off = next((r for r in rows if r["type"] == "digitalocean.droplet_off_billed"), {})
    check("its detail names the monthly price", "$24" in off.get("detail", ""), off.get("detail", ""))
    down = next((r for r in rows if r["type"] == "digitalocean.app_down"), {})
    check("an app with no active deployment is a finding tied to its project through the registry",
          down and "shop" in down.get("detail", "") and "ERROR" in down.get("detail", ""), str(down))
    check("an active Droplet and a serving app raise nothing",
          types.count("digitalocean.droplet_off_billed") == 1 and types.count("digitalocean.app_down") == 1)
    doc["teams"][0]["apps"].append({"id": "app-4", "name": "first", "serving": False, "repositories": [],
                                    "last_phase": "BUILDING"})
    building = F.findings(doc, repos, relations, today=date(2026, 10, 20))
    check("an app whose first deployment is still building is not flagged",
          not any(r["subject"].endswith(":app-4") for r in building))
    stale = F.findings(doc, repos, relations, today=date(2026, 10, 25))
    check("a stale scan says only that it is stale",
          [r["type"] for r in stale] == ["digitalocean.snapshot_stale"], str(stale)[:200])
    check("no scan, no rows", F.findings(None, [], []) == [])
    untied = F.findings(doc, [], [], today=date(2026, 10, 20))
    check("an app whose repository the registry does not hold is named as belonging to no project",
          any("no project" in r["detail"] for r in untied if r["type"] == "digitalocean.app_down"))


def main() -> int:
    print("digitalocean door: teams, tokens and the collector (OBS-155)")
    provider = Provider()
    server, base = serve(provider)
    try:
        with tempfile.TemporaryDirectory() as t:
            tmp = pathlib.Path(t).resolve()
            env = environment(tmp, base)
            case_stash_teams_and_members(tmp, env, provider)
            case_team_create(env, provider)
            case_issue_install_rotate_revoke(tmp, env, provider)
            case_api_override_is_loopback_only(env)
            case_review_round_1(tmp, env, provider)
            # The collector reads with an observatory-read token where one is installed.
            r = door(env, "rotate", "observatory-read--example-team", stdin=READ2 + "\n")
            check("the collector's read token is replaced by rotate", r.returncode == 0, (r.stdout + r.stderr)[-300:])
            case_collector(tmp, env, provider)
        case_findings(provider)
    finally:
        server.shutdown()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
