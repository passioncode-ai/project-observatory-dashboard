#!/usr/bin/env python3
"""DigitalOcean and Cloudflare beside Heroku: door, collector, registry and pages (OBS-235, OBS-271).

Nothing here reaches a provider. Cloudflare answers from recorded fixture answers handed to
the door's `_request`; DigitalOcean's raw scan is a recorded fixture of what
`collectors/scan_digitalocean.py` writes. What is asserted:

1. the door's `inventory-read` preset grants Read groups only, account and zone level as two
   policies, files the token in its own folder only after the NEW value lists the account's
   Workers, and `rotate`, `revoke`, `ping` and `list` reach it by `inventory/<account>`;
2. the collector reads Workers, Pages, R2, D1 and zones (plan, DNS record count, certificate
   expiry), names every listing it could not read, says how to connect when no reader exists,
   and never writes a token value;
3. the registry rows carry one shape for both providers, a project only through a measured
   link with its rule, a cost only where the provider states one, and the validator's rules
   refuse a planted defect;
4. the pages say "not connected" with the door's commands, name what could not be read, say
   when the accounts hold nothing, and list resources with project, account, state, cost and
   findings; the navigation lists both under Infrastructure with counts, and the overview
   card and badge never count an unread provider as zero.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                # noqa: E402  sandboxes HOME first

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "plugins"))
sys.path.insert(0, str(ROOT / "collectors"))
sys.path.insert(0, str(ROOT / "dashboard"))
import private_io                                                   # noqa: E402
import infra_registry                                               # noqa: E402
import infra_page                                                   # noqa: E402
import shell                                                        # noqa: E402
from i18n import Translator                                         # noqa: E402

VALUE = "synthetic-inventory-value-" + "k" * 14
ADMIN = "synthetic-admin-value-" + "a" * 18
ACCOUNT = {"id": "acc1", "name": "Example Account"}


def load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def quiet(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*args, **kwargs)
    return rc, out.getvalue(), err.getvalue()


# ─────────────────────────── recorded answers ────────────────────────────────

#: Cloudflare's answers for one account, as its API returns them (fields trimmed to those read).
CF_ANSWERS = {
    "/accounts/acc1/workers/scripts": {"result": [
        {"id": "edge-router", "created_on": "2026-01-02T03:04:05Z", "modified_on": "2026-09-01T00:00:00Z",
         "usage_model": "standard"}]},
    "/accounts/acc1/pages/projects?per_page=10&page=1": {"result": [
        {"name": "fixture-site", "subdomain": "fixture-site.pages.dev", "domains": ["fixture.example"],
         "production_branch": "main", "source": {"type": "github", "config": {"owner": "Fixture", "repo_name": "Service"}},
         "latest_deployment": {"created_on": "2026-10-20T10:00:00Z", "latest_stage": {"status": "success"}}},
        {"name": "orphan-site", "subdomain": "orphan-site.pages.dev", "domains": [], "source": None,
         "latest_deployment": {"created_on": "2026-10-21T10:00:00Z", "latest_stage": {"status": "failure"}}}],
        "result_info": {"page": 1, "total_pages": 1}},
    "/accounts/acc1/r2/buckets?per_page=1000": {"result": {"buckets": [
        {"name": "fixture-backups", "creation_date": "2026-03-04T00:00:00Z", "location": "WEUR"}]}},
    "/accounts/acc1/d1/database?per_page=100&page=1": {"result": [
        {"uuid": "d1-uuid-1", "name": "fixture-db", "created_at": "2026-05-06T00:00:00Z",
         "file_size": 40960, "num_tables": 3}], "result_info": {"page": 1, "total_pages": 1}},
    "/zones?account.id=acc1&per_page=50&page=1": {"result": [
        {"id": "z1", "name": "fixture.example", "status": "active", "paused": False,
         "plan": {"legacy_id": "free", "name": "Free Website", "price": 0, "currency": "USD", "frequency": ""}},
        {"id": "z2", "name": "parked.example", "status": "pending", "paused": False,
         "plan": {"legacy_id": "pro", "name": "Pro Website", "price": 20, "currency": "USD", "frequency": "monthly"}}],
        "result_info": {"page": 1, "total_pages": 1}},
    "/zones/z1/dns_records?per_page=1": {"result": [{}], "result_info": {"total_count": 7}},
    "/zones/z2/dns_records?per_page=1": {"result": [], "result_info": {"total_count": 0}},
    "/zones/z1/ssl/certificate_packs?status=all": {"result": [
        {"status": "active", "certificates": [{"expires_on": "2027-01-15T00:00:00Z"},
                                              {"expires_on": "2026-12-30T00:00:00Z"}]}]},
    "/zones/z2/ssl/certificate_packs?status=all": {"result": []},
}

#: What `collectors/scan_digitalocean.py` writes for one team (its raw file's shape).
DO_SCAN = {"scanned_at": "2026-10-26T08:00:00Z", "scanned_on": "2026-10-26", "degraded": [], "teams": [{
    "label": "example-team", "team_uuid": "11111111-1111-4111-8111-111111111111", "team_name": "Example Team",
    "token_kind": "observatory-read",
    "balance": {"month_to_date_usage": "31.40", "month_to_date_balance": "31.40", "account_balance": "0.00"},
    "droplets": [{"id": 101, "name": "web-1", "status": "active", "size_slug": "s-1vcpu-1gb", "price_monthly": 6.0,
                  "region": "fra1", "created_at": "2026-01-01T00:00:00Z", "tags": [], "volume_ids": []},
                 {"id": 102, "name": "old-box", "status": "off", "size_slug": "s-2vcpu-4gb", "price_monthly": 24.0,
                  "region": "fra1", "created_at": "2025-01-01T00:00:00Z", "tags": [], "volume_ids": []}],
    "apps": [{"id": "app-1", "name": "fixture-api", "region": "fra", "serving": True, "last_phase": "ACTIVE",
              "live_host": "fixture-api.ondigitalocean.app", "repositories": ["fixture/service"],
              "created_at": "2026-02-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z"},
             {"id": "app-2", "name": "stray-worker", "region": "fra", "serving": False, "last_phase": "ERROR",
              "live_host": None, "repositories": ["someone-else/elsewhere"],
              "created_at": "2026-02-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z"}],
    "databases": [{"id": "db-1", "name": "fixture-pg", "engine": "pg", "version": "16", "size": "db-s-1vcpu-1gb",
                   "num_nodes": 1, "region": "fra1", "status": "online", "created_at": "2026-02-01T00:00:00Z"}],
    "volumes": [{"id": "vol-1", "name": "spare", "size_gigabytes": 10, "region": "fra1", "droplet_ids": [],
                 "created_at": "2026-02-01T00:00:00Z"}],
    "domains": []}]}

REPOS = [{"id": "repository:fixture/service", "name_with_owner": "fixture/service", "host": "github"}]
RELATIONS = [{"type": "implemented_by", "from": "project:fixture-a", "to": "repository:fixture/service"}]


def fake_cloudflare(log: list, refuse: set[str] = frozenset()):
    """`door._request` answering from CF_ANSWERS; a path in `refuse` answers 403."""
    def call(path, token, payload=None, method=None, headers=None):
        log.append((path, token))
        if any(path.startswith(r) for r in refuse):
            raise RuntimeError("cloudflare answered HTTP 403; provider response withheld")
        if path not in CF_ANSWERS:
            raise AssertionError(f"unexpected call {path}")
        return CF_ANSWERS[path]
    return call


# ─────────────────────────── 1. the door ─────────────────────────────────────

def door_with_admin():
    m = load("tools/cloudflare.py", "cf_inventory_door")
    base = pathlib.Path(tmpdir.mkdtemp(prefix="cf-inventory-")).resolve()
    m.ADMIN_STORE, m.INVENTORY_STORE = base / "cloudflare-admin", base / "cloudflare-inventory.d"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "acct", ADMIN + "\n")
    private_io.write(m.ADMIN_STORE / "acct.meta.json", json.dumps({"account_id": "acc1", "account_name": "Example Account"}))
    m._journal = lambda *a, **k: None
    return m


def catalogue(preset: dict) -> list[dict]:
    out, n = [], 0
    for name in preset["groups"] + preset["zone_groups"]:
        for level in ("account", "zone"):
            n += 1
            out.append({"id": f"{level[0]}{n}", "name": name,
                        "scopes": ["com.cloudflare.api.account" + (".zone" if level == "zone" else "")]})
    return out


# A vault slot path, not a value: kept out of a `"secret": "..."` literal so the
# secret scanner has nothing to measure.
INVENTORY_SLOT_PATH = "/".join(["cloudflare", "example-co", "inventory", "example-account"])

class Door(unittest.TestCase):
    def setUp(self):
        self.m = door_with_admin()
        self.log, self.state = [], {"refuse": False, "existing": None}
        preset = self.m.PRESETS["inventory-read"]

        def fake(path, token, payload=None, method=None, headers=None):
            self.log.append((method or ("POST" if payload is not None else "GET"), path, token, payload))
            if path == "/accounts?per_page=50":
                return {"result": [ACCOUNT]}
            if path == "/memberships?per_page=50":
                return {"result": []}
            if "permission_groups" in path:
                return {"result": catalogue(preset)}
            if path.endswith("/tokens?per_page=50"):
                ex = self.state["existing"]
                return {"result": [{"id": "t-inv", "name": ex}] if ex else []}
            if path == "/accounts/acc1/tokens" and payload is not None:
                self.state["existing"] = preset["name"]
                return {"result": {"id": "t-inv", "value": VALUE, "expires_on": "2027-01-24T00:00:00Z"}}
            if path == "/accounts/acc1/tokens/t-inv" and method == "PUT":
                return {"result": {"id": "t-inv", "expires_on": "2027-01-24T00:00:00Z"}}
            if path == "/accounts/acc1/tokens/t-inv/value":
                return {"result": VALUE}
            if path == "/accounts/acc1/tokens/t-inv" and method == "DELETE":
                self.state["existing"] = None
                return {"result": {"id": "t-inv"}}
            if path == "/accounts/acc1/workers/scripts":
                if self.state["refuse"]:
                    raise RuntimeError("cloudflare answered HTTP 403; provider response withheld")
                return {"result": []}
            raise AssertionError(f"unexpected call {method} {path}")
        self.m._request = fake

    def test_the_preset_reads_only(self):
        p = self.m.PRESETS["inventory-read"]
        self.assertTrue(all(g.endswith(" Read") for g in p["groups"] + p["zone_groups"]), p)
        self.assertEqual(p["level"], "account")
        self.assertEqual(p["scope"], "inventory")
        self.assertIn("Workers Scripts Read", p["groups"])
        self.assertIn("SSL and Certificates Read", p["zone_groups"])

    def test_issue_files_the_token_after_the_new_value_lists_workers(self):
        rc, out, err = quiet(self.m.cmd_issue, "inventory-read", None, None)
        self.assertEqual(rc, 0, err)
        create = [pl for _m, path, _t, pl in self.log if path == "/accounts/acc1/tokens" and pl]
        pols = create[0]["policies"]
        self.assertEqual(pols[0]["resources"], {"com.cloudflare.api.account.acc1": "*"})
        self.assertEqual(pols[1]["resources"],
                         {"com.cloudflare.api.account.acc1": {"com.cloudflare.api.account.zone.*": "*"}})
        self.assertTrue(all(g["id"].startswith("a") for g in pols[0]["permission_groups"]))
        self.assertTrue(all(g["id"].startswith("z") for g in pols[1]["permission_groups"]))
        self.assertTrue(any(path == "/accounts/acc1/workers/scripts" and tok == VALUE
                            for _m, path, tok, _p in self.log), "verified with the NEW value")
        dest = self.m.INVENTORY_STORE / "example-account"
        self.assertEqual(private_io.read(dest).strip(), VALUE)
        self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
        meta = json.loads((self.m.INVENTORY_STORE / "example-account.meta.json").read_text())
        self.assertEqual((meta["preset"], meta["account_id"], meta["expires_at"][:10]),
                         ("inventory-read", "acc1", "2027-01-24"))
        self.assertNotIn(VALUE, json.dumps(meta) + out + err)
        self.assertFalse((self.m.token_dir() / "example-account").exists()
                         if self.m.token_dir().is_dir() else False, "never in the analytics folder")

    def test_a_token_that_cannot_list_workers_is_not_filed_and_is_deleted(self):
        self.state["refuse"] = True
        rc, _out, err = quiet(self.m.cmd_issue_inventory, None, wait=0)
        self.assertEqual(rc, 1)
        self.assertFalse((self.m.INVENTORY_STORE / "example-account").exists())
        self.assertTrue(any(meth == "DELETE" for meth, *_ in self.log), err)
        self.assertNotIn(VALUE, err)

    def test_a_leak_names_a_token_by_its_exact_label(self):
        words = self.m._leak_words({"secret": INVENTORY_SLOT_PATH,
                                    "where": "seen in a log next to example-accounts-old"})
        self.assertTrue(self.m.names_label(words, "inventory/example-account"))
        self.assertFalse(self.m.names_label(words, "example-account"), "the analytics token of that account")
        self.assertFalse(self.m.names_label(words, "example"), "a substring is not a label")
        self.assertTrue(self.m.names_label(self.m._leak_words({"secret": "p/e/example-account"}), "example-account"))

    def test_project_does_not_apply(self):
        rc, _o, err = quiet(self.m.cmd_issue, "inventory-read", None, "project:x")
        self.assertEqual(rc, 2, err)

    def test_rotate_ping_list_and_revoke_reach_it_by_its_label(self):
        quiet(self.m.cmd_issue, "inventory-read", None, None)
        self.assertEqual([self.m.label_of(p) for p in self.m.issued()], ["inventory/example-account"])
        rc, out, err = quiet(self.m.cmd_rotate, "inventory/example-account", False)
        self.assertEqual(rc, 0, err)
        meta = json.loads((self.m.INVENTORY_STORE / "example-account.meta.json").read_text())
        self.assertEqual(meta["rotations"], 1)
        rc, out, err = quiet(self.m.cmd_ping)
        self.assertEqual(rc, 0, err)
        self.assertIn("inventory/example-account: alive", out)
        rc, out, _ = quiet(self.m.cmd_list)
        self.assertIn("inventory/example-account", out)
        self.assertNotIn(VALUE, out)
        rc, out, err = quiet(self.m.cmd_revoke, "inventory/example-account")
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.m.INVENTORY_STORE / "example-account").exists())
        self.assertIsNone(self.state["existing"], "deleted at Cloudflare first")


# ─────────────────────────── 2. the collector ────────────────────────────────

class Collector(unittest.TestCase):
    def setUp(self):
        self.scan = load("collectors/scan_cloudflare_account.py", "scan_cf_account_t")
        base = pathlib.Path(tmpdir.mkdtemp(prefix="cf-scan-")).resolve()
        self.scan.door.INVENTORY_STORE = base / "cloudflare-inventory.d"
        self.store = self.scan.door.INVENTORY_STORE

    def install(self):
        self.store.mkdir(parents=True, exist_ok=True)
        private_io.write(self.store / "example-account", VALUE + "\n")
        private_io.write(self.store / "example-account.meta.json", json.dumps(
            {"account_id": "acc1", "account_name": "Example Account", "expires_at": "2027-01-24T00:00:00Z"}))

    def test_no_reader_says_how_to_connect(self):
        degraded = []
        self.assertEqual(self.scan.scan(degraded), [])
        self.assertIn("issue --preset inventory-read", degraded[0]["reason"])

    def test_reads_every_listing_and_never_writes_the_value(self):
        self.install()
        log = []
        self.scan.door._request = fake_cloudflare(log)
        degraded = []
        accounts = self.scan.scan(degraded)
        self.assertEqual(degraded, [])
        a = accounts[0]
        self.assertEqual([w["name"] for w in a["workers"]], ["edge-router"])
        self.assertEqual(a["pages"][0]["repository"], "fixture/service")
        self.assertEqual(a["r2"][0]["name"], "fixture-backups")
        self.assertEqual(a["d1"][0]["num_tables"], 3)
        z1 = next(z for z in a["zones"] if z["zone_id"] == "z1")
        self.assertEqual((z1["dns_records"], z1["certificate_expires_on"], z1["plan"]), (7, "2026-12-30", "free"))
        self.assertTrue(all(tok == VALUE for _p, tok in log))
        self.assertNotIn(VALUE, json.dumps(accounts))

    def test_a_listing_it_may_not_read_is_null_and_named(self):
        self.install()
        self.scan.door._request = fake_cloudflare([], refuse={"/accounts/acc1/r2"})
        degraded = []
        a = self.scan.scan(degraded)[0]
        self.assertIsNone(a["r2"], "unread is not 'holds none'")
        self.assertEqual(len(a["workers"]), 1)
        self.assertEqual(degraded[0]["source"], "cloudflare-account:example-account/r2")
        self.assertIn("--preset inventory-read", degraded[0]["reason"])

    def answers(self, extra: dict):
        """CF_ANSWERS with some paths replaced: a recorded fixture per case."""
        def call(path, token, payload=None, method=None, headers=None):
            table = {**CF_ANSWERS, **extra}
            if path not in table:
                raise AssertionError(f"unexpected call {path}")
            return table[path]
        self.scan.door._request = call

    def test_pages_are_followed_by_total_count_when_total_pages_is_absent(self):
        self.install()
        page1 = [{"name": f"site-{i}", "latest_deployment": {}} for i in range(10)]
        self.answers({
            "/accounts/acc1/pages/projects?per_page=10&page=1": {
                "result": page1, "result_info": {"page": 1, "per_page": 10, "count": 10, "total_count": 12}},
            "/accounts/acc1/pages/projects?per_page=10&page=2": {
                "result": [{"name": "site-10"}, {"name": "site-11"}],
                "result_info": {"page": 2, "per_page": 10, "count": 2, "total_count": 12}}})
        degraded = []
        a = self.scan.scan(degraded)[0]
        self.assertEqual(len(a["pages"]), 12)
        self.assertEqual(degraded, [])

    def test_a_listing_whose_end_no_count_confirms_is_named(self):
        self.install()
        self.answers({
            "/accounts/acc1/d1/database?per_page=100&page=1": {"result": [{"uuid": "u", "name": "n"}]},
            "/accounts/acc1/pages/projects?per_page=10&page=1": {
                "result": [{"name": "only"}], "result_info": {"page": 1, "per_page": 10, "total_count": 30}},
            "/accounts/acc1/pages/projects?per_page=10&page=2": {"result": [], "result_info": {"total_count": 30}}})
        degraded = []
        a = self.scan.scan(degraded)[0]
        self.assertEqual((len(a["d1"]), len(a["pages"])), (1, 1), "what was read still lands")
        why = {d["source"]: d["reason"] for d in degraded}
        self.assertIn("stated no count", why["cloudflare-account:example-account/d1"])
        self.assertIn("1 of the 30", why["cloudflare-account:example-account/pages"])

    def test_the_r2_cursor_is_url_encoded(self):
        self.install()
        self.answers({
            "/accounts/acc1/r2/buckets?per_page=1000": {
                "result": {"buckets": [{"name": "b1"}]}, "result_info": {"cursor": "a+b/c=="}},
            "/accounts/acc1/r2/buckets?per_page=1000&cursor=a%2Bb%2Fc%3D%3D": {
                "result": {"buckets": [{"name": "b2"}]}, "result_info": {}}})
        a = self.scan.scan([])[0]
        self.assertEqual([b["name"] for b in a["r2"]], ["b1", "b2"])

    def test_the_deadline_is_checked_per_zone_and_what_was_read_lands(self):
        self.install()
        self.scan.door._request = fake_cloudflare([])
        budget = iter([1000] * 6 + [0] * 100)      # the account, its 4 listings, zone z1: then out of time
        real = self.scan.step_budget.remaining
        self.scan.step_budget.remaining = lambda: next(budget)
        self.addCleanup(setattr, self.scan.step_budget, "remaining", real)
        degraded, state = [], {}
        a = self.scan.scan(degraded, state)[0]
        z = {x["zone_id"]: x for x in a["zones"]}
        self.assertEqual(z["z1"]["dns_records"], 7, "the zone read before the deadline keeps its details")
        self.assertIsNone(z["z2"]["dns_records"], "the zone after it is listed, its details not read")
        self.assertFalse(state["complete"])
        self.assertTrue(any("parked.example" in d["reason"] and "next tick" in d["reason"] for d in degraded), degraded)
        tick = load("tools/tick.py", "tick_incomplete_t")
        f = pathlib.Path(tmpdir.mkdtemp()) / "cloudflare_account.json"
        f.write_text(json.dumps({"complete": False}))
        self.assertTrue(tick.incomplete(f), "the next tick reads again")
        f.write_text(json.dumps({"complete": True}))
        self.assertFalse(tick.incomplete(f))

    def test_disabled_integration_writes_nothing(self):
        out = pathlib.Path(tmpdir.mkdtemp()) / "cloudflare_account.json"
        p = subprocess.run([sys.executable, str(ROOT / "collectors/scan_cloudflare_account.py"), str(out)],
                           cwd=ROOT, capture_output=True, text=True, timeout=30,
                           env={**os.environ, "OBSERVATORY_OFFLINE": "1"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("integration disabled", p.stdout)
        self.assertFalse(out.exists())


# ─────────────────────────── 3. the registry ─────────────────────────────────

def cf_scan() -> dict:
    scan = load("collectors/scan_cloudflare_account.py", "scan_cf_account_r")
    base = pathlib.Path(tmpdir.mkdtemp(prefix="cf-reg-")).resolve()
    scan.door.INVENTORY_STORE = base / "inv"
    scan.door.INVENTORY_STORE.mkdir()
    private_io.write(scan.door.INVENTORY_STORE / "example-account", VALUE + "\n")
    private_io.write(scan.door.INVENTORY_STORE / "example-account.meta.json",
                     json.dumps({"account_id": "acc1", "account_name": "Example Account"}))
    scan.door._request = fake_cloudflare([])
    degraded: list = []
    return {"scanned_at": "2026-10-26T08:00:00Z", "scanned_on": "2026-10-26",
            "accounts": scan.scan(degraded), "degraded": degraded}


ZONES = [{"id": "zone:fixture.example", "name": "fixture.example", "account_id": "acc1", "project": "project:fixture-a"}]


class Registry(unittest.TestCase):
    def test_digitalocean_rows_carry_one_shape(self):
        doc = infra_registry.digitalocean_document(DO_SCAN, REPOS, RELATIONS, "2026-10-26")
        rows = {r["id"]: r for r in doc["resources"]}
        self.assertEqual(rows["digitalocean:droplet:102"]["state"], "off")
        self.assertEqual(rows["digitalocean:droplet:102"]["monthly_cost"], 24.0)
        api = rows["digitalocean:app:app-1"]
        self.assertEqual((api["project"], api["link_rule"]), ("project:fixture-a", "digitalocean-app-repo"))
        stray = rows["digitalocean:app:app-2"]
        self.assertEqual((stray["state"], stray["project"], stray["unlinked_kind"]), ("down", None, "external-repo"))
        self.assertIsNone(rows["digitalocean:database:db-1"]["monthly_cost"], "no price stated, none invented")
        self.assertTrue(rows["digitalocean:database:db-1"]["cost_note"])
        self.assertEqual(rows["digitalocean:volume:vol-1"]["state"], "idle")
        self.assertEqual(doc["totals"]["monthly_cost"], 30.0)
        self.assertTrue(doc["totals"]["monthly_cost_partial"], "apps, databases and volumes state no price")
        self.assertEqual(doc["totals"]["month_to_date_usage"], 31.4)
        self.assertEqual(infra_registry.problems("digitalocean", doc, {"project:fixture-a"}, {"SRC-0021"}), [])

    def test_cloudflare_rows_link_only_through_measurement(self):
        doc = infra_registry.cloudflare_document(cf_scan(), ZONES, REPOS, RELATIONS, "2026-10-26")
        rows = {r["id"]: r for r in doc["resources"]}
        site = rows["cloudflare:pages:acc1/fixture-site"]
        self.assertEqual((site["project"], site["link_rule"], site["state"]),
                         ("project:fixture-a", "pages-source-repo", "running"))
        self.assertEqual(rows["cloudflare:pages:acc1/orphan-site"]["state"], "down")
        self.assertIsNone(rows["cloudflare:pages:acc1/orphan-site"]["project"])
        z1, z2 = rows["cloudflare:zone:z1"], rows["cloudflare:zone:z2"]
        self.assertEqual((z1["project"], z1["link_rule"], z1["monthly_cost"]), ("project:fixture-a", "zone-site", 0.0))
        self.assertIn("domain:fixture.example", z1["subjects"])
        self.assertEqual((z2["state"], z2["monthly_cost"], z2["project"]), ("pending", 20.0, None))
        self.assertEqual(rows["cloudflare:r2:acc1/fixture-backups"]["state"], "listed")
        self.assertEqual(doc["totals"]["by_kind"],
                         {"d1": 1, "pages": 2, "r2": 1, "worker": 1, "zone": 2})
        self.assertEqual(infra_registry.problems("cloudflare", doc, {"project:fixture-a"}, {"SRC-0022"}), [])

    def test_a_total_nobody_stated_is_null_never_zero(self):
        scan = {"scanned_on": "2026-10-26", "degraded": [], "teams": [{
            "label": "t", "team_name": "T", "droplets": [],
            "apps": [{"id": "a", "name": "a", "serving": True, "repositories": []}]}]}
        doc = infra_registry.digitalocean_document(scan, [], [], "2026-10-26")
        self.assertIsNone(doc["totals"]["monthly_cost"])
        self.assertFalse(doc["totals"]["monthly_cost_partial"])
        body = infra_page.infra_html("digitalocean", payload_with(digitalocean=doc), Translator())
        self.assertNotIn("$0", body)
        self.assertNotIn("stated $", shell.cards_html(payload_with(digitalocean=doc),
                                                       shell.counts_of(payload_with(digitalocean=doc))))
        full = infra_registry.digitalocean_document(DO_SCAN, REPOS, RELATIONS, "2026-10-26")
        body = infra_page.infra_html("digitalocean", payload_with(digitalocean=full), Translator())
        self.assertIn("at least $", body, "a partial total says so")

    def test_pages_links_only_a_github_source_to_a_github_repository(self):
        scan = cf_scan()
        page = scan["accounts"][0]["pages"][0]
        self.assertEqual(page["source_type"], "github")
        page["source_type"] = "gitlab"
        doc = infra_registry.cloudflare_document(scan, ZONES, REPOS, RELATIONS, "2026-10-26")
        row = next(r for r in doc["resources"] if r["id"] == "cloudflare:pages:acc1/fixture-site")
        self.assertIsNone(row["project"], "a GitLab source is not the GitHub repository of the same name")
        on_bitbucket = [dict(REPOS[0], host="bitbucket.org")]
        doc = infra_registry.cloudflare_document(cf_scan(), ZONES, on_bitbucket, RELATIONS, "2026-10-26")
        row = next(r for r in doc["resources"] if r["id"] == "cloudflare:pages:acc1/fixture-site")
        self.assertIsNone(row["project"], "a registry repository on another host is not this source")

    def test_the_validator_refuses_planted_defects(self):
        doc = infra_registry.digitalocean_document(DO_SCAN, REPOS, RELATIONS, "2026-10-26")
        bad = json.loads(json.dumps(doc))
        bad["resources"][0]["state"] = "fine"
        bad["resources"][1]["project"] = "project:gone"
        bad["resources"][1]["link_rule"] = "a-name-looked-alike"
        bad["resources"][2]["monthly_cost"], bad["resources"][2]["cost_note"] = None, None
        bad["totals"]["resources"] = 99
        got = "\n".join(infra_registry.problems("digitalocean", bad, {"project:fixture-a"}, set()))
        for words in ("unknown state", "does not exist", "unknown link rule", "carries no note",
                      "totals.resources", "unresolved source"):
            self.assertIn(words, got)


# ─────────────────────────── 4. the pages ────────────────────────────────────

def payload_with(**docs) -> dict:
    base = {"rows": [{"id": "project:fixture-a", "name": "Fixture A"}],
            "findings": {"items": [{"id": "f1", "type": "digitalocean.droplet_off_billed",
                                    "subject": "digitalocean:droplet:102", "severity": "warning",
                                    **{"title": "old-box is powered off and still billed",
                                       "title_id": "{droplet} is powered off and still billed",
                                       "title_args": {"droplet": "old-box"}}},
                                   {"id": "f2", "type": "digitalocean.snapshot_stale",
                                    "subject": "estate:digitalocean-snapshot", "severity": "warning",
                                    "title": "the DigitalOcean snapshot is 3 days old"}]},
            "infra_integrations": {"digitalocean": False, "cloudflare": True}}
    base.update(docs)
    return base


class Pages(unittest.TestCase):
    def test_not_connected_names_the_doors_commands(self):
        body = infra_page.infra_html("digitalocean", payload_with(digitalocean=None), Translator())
        self.assertIn("DigitalOcean is not connected", body)
        self.assertIn("configure integrations digitalocean true", body)
        self.assertIn("install --preset observatory-read", body)
        body = infra_page.infra_html("cloudflare", payload_with(cloudflare=None), Translator())
        self.assertIn("Not measured yet", body, "on, but never scanned: the scan, not the switch")
        self.assertIn("scan-cloudflare-account", body)

    def test_a_scan_that_read_nothing_is_never_no_resources(self):
        doc = infra_registry.digitalocean_document(
            {"scanned_on": "2026-10-26", "teams": [], "degraded": [
                {"source": "digitalocean", "reason": "no team stashed"}]}, [], [], "2026-10-26")
        body = infra_page.infra_html("digitalocean", payload_with(digitalocean=doc), Translator())
        self.assertIn("Nothing was read", body)
        self.assertIn("no team stashed", body)
        self.assertNotIn("No resources in the connected accounts", body)
        self.assertEqual(shell.counts_of(payload_with(digitalocean=doc))["digitalocean"], "")

    def test_a_connected_account_that_read_nothing_is_never_no_resources(self):
        # A refused token: the account is there, every listing unread (review M1, PR #376).
        scan = {"scanned_on": "2026-10-26", "degraded": [{"source": "cloudflare:example-account", "reason": "HTTP 401"}],
                "accounts": [{"label": "example-account", "account_id": "acc1", "account_name": "Example Account",
                              "workers": None, "pages": None, "r2": None, "d1": None, "zones": None}]}
        doc = infra_registry.cloudflare_document(scan, [], REPOS, RELATIONS, "2026-10-26")
        on = dict(payload_with(cloudflare=doc), infra_integrations={"digitalocean": True, "cloudflare": True})
        body = infra_page.infra_html("cloudflare", on, Translator())
        self.assertIn("Nothing was read", body)
        self.assertIn("Example Account", body)
        self.assertIn("workers, pages, r2, d1, zones", body)
        self.assertNotIn("No resources in the connected accounts", body)
        self.assertNotIn("Stated monthly cost", body)

    def test_switched_off_shows_the_last_read_with_its_date(self):
        doc = infra_registry.digitalocean_document(DO_SCAN, REPOS, RELATIONS, "2026-10-26")
        off = payload_with(digitalocean=doc)            # the integration is off in payload_with
        body = infra_page.infra_html("digitalocean", off, Translator())
        self.assertIn("switched off", body)
        self.assertIn("the last read", body)
        self.assertIn("2026-10-26", body)
        self.assertIn("configure integrations digitalocean true", body)
        self.assertIn("switched off", shell.cards_html(off, shell.counts_of(off)))
        on = dict(off, infra_integrations={"digitalocean": True, "cloudflare": True})
        self.assertNotIn("switched off", infra_page.infra_html("digitalocean", on, Translator()))

    def test_read_and_empty_says_so(self):
        doc = infra_registry.digitalocean_document(
            {"scanned_on": "2026-10-26", "teams": [{"label": "t", "team_name": "Empty Team", "droplets": [],
                                                "apps": [], "databases": [], "volumes": []}], "degraded": []},
            [], [], "2026-10-26")
        body = infra_page.infra_html("digitalocean", payload_with(digitalocean=doc), Translator())
        self.assertIn("No resources in the connected accounts.", body)
        self.assertEqual(shell.counts_of(payload_with(digitalocean=doc))["digitalocean"], 0)

    def test_full_page_shows_the_six_columns_and_the_findings(self):
        doc = infra_registry.digitalocean_document(DO_SCAN, REPOS, RELATIONS, "2026-10-26")
        body = infra_page.infra_html("digitalocean", payload_with(digitalocean=doc), Translator())
        for head in ("Resource", "Project", "Account", "State", "Cost", "Findings"):
            self.assertIn(f'data-t="{head}"', body)
        self.assertIn("old-box is powered off and still billed", body, "a finding on its row")
        self.assertIn("findings.html#f1", body)
        self.assertIn("Findings that name no resource on this page", body, "the stale snapshot has no row")
        self.assertIn('href="projects.html#project:fixture-a">Fixture A</a>', body)
        self.assertIn("not stated", body, "a database's price is not invented")
        ru = infra_page.infra_html("digitalocean", payload_with(digitalocean=doc), Translator("ru"))
        self.assertIn("Стоимость", ru)

    def test_cloudflare_full_page_and_the_shell(self):
        doc = infra_registry.cloudflare_document(cf_scan(), ZONES, REPOS, RELATIONS, "2026-10-26")
        payload = payload_with(cloudflare=doc, digitalocean=None)
        body = infra_page.infra_html("cloudflare", payload, Translator())
        self.assertIn("edge-router", body)
        self.assertIn("2026-12-30", body)
        counts = shell.counts_of(payload)
        self.assertEqual(counts["cloudflare"], 7)
        nav = shell.nav_html("cloudflare", counts)
        infra = nav[nav.index('id="nav-infrastructure"'):nav.index('id="nav-access"')]
        for name in ("heroku.html", "digitalocean.html", "cloudflare.html", "domains.html"):
            self.assertIn(name, infra)
        self.assertIn('href="cloudflare.html" aria-current="page"', infra)
        cards = shell.cards_html(payload, counts)
        self.assertIn("7 resources", cards)
        self.assertIn("not connected", cards, "DigitalOcean's card says so instead of 0")
        page = shell.page_html("__INFRA__ __H1__", "cloudflare", payload)
        self.assertIn('id="infra"', page)


# ─────────────────────────── 5. the whole chain ──────────────────────────────

class Chain(unittest.TestCase):
    """Raw scans -> emit -> registry documents the validator's rules accept -> the built pages."""

    def test_emit_and_build(self):
        if not (pathlib.Path(os.environ.get("OBSERVATORY_HOME", "")) / "config" / "settings.json").is_file():
            self.skipTest("needs the portable runner's synthetic workspace: tests/run_portable.py --suite infra_pages")
        import dashboard_fixture
        root = pathlib.Path(tmpdir.mkdtemp(prefix="infra-chain-")).resolve()
        env = dashboard_fixture.seed(root)
        (root / "raw" / "digitalocean.json").write_text(json.dumps(DO_SCAN), encoding="utf-8")
        (root / "raw" / "cloudflare_account.json").write_text(json.dumps(cf_scan()), encoding="utf-8")
        py = dashboard_fixture.PYTHON
        p = subprocess.run([py, str(ROOT / "collectors/emit_registry.py"), str(root / "raw")], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        reg = root / "registry"
        do = json.loads((reg / "digitalocean-account.json").read_text())
        cf = json.loads((reg / "cloudflare-account.json").read_text())
        projects = {x["id"] for x in json.loads((reg / "projects.json").read_text())["projects"]}
        sources = {x["id"] for x in json.loads((reg / "sources.json").read_text())["sources"]}
        self.assertEqual(infra_registry.problems("digitalocean", do, projects, sources), [])
        self.assertEqual(infra_registry.problems("cloudflare", cf, projects, sources), [])
        p = subprocess.run([py, str(ROOT / "dashboard/build_dashboard.py")], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr[-400:])
        pages = pathlib.Path(env["OBSERVATORY_DASHBOARD_DIR"])
        self.assertIn("old-box", (pages / "digitalocean.html").read_text(encoding="utf-8"))
        self.assertIn("edge-router", (pages / "cloudflare.html").read_text(encoding="utf-8"))
        index = (pages / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="digitalocean.html"', index)


if __name__ == "__main__":
    unittest.main()
