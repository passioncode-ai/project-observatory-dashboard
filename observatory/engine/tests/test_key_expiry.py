#!/usr/bin/env python3
"""Every door that can mint with an expiry does, and records it (OBS-191, part 1).

Nothing here reaches a provider: Cloudflare, OpenRouter and Heroku are fakes
answering the calls each door makes, and the vault is a scratch store under
`OBSERVATORY_VAULT_DIR`. What is asserted:

- the expiry travels in the provider's own field (`expires_on`, `expires_at`,
  `expires_in`), 90 days out unless asked otherwise, and a lifetime the door will
  not mint is refused before any call;
- the expiry is recorded where the door already keeps the credential's
  metadata (the slot's `meta.json`, the ledger, the token's record), never the
  value; a credential without one records null and why;
- the places keys are listed say it: `vault.py list`, `use_secret.py names`,
  `observatory_credentials` and each door's `list`.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
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

VAULT_DIR = pathlib.Path(tmpdir.mkdtemp(prefix="expiry-vault-")).resolve() / "projects"
os.environ["OBSERVATORY_VAULT_DIR"] = str(VAULT_DIR)
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "plugins"))
import key_expiry                                                   # noqa: E402
import private_io                                                   # noqa: E402
import vault                                                        # noqa: E402

NOW = datetime.datetime(2026, 10, 26, 12, 0, 0, tzinfo=datetime.timezone.utc)
VALUE = "synthetic-minted-value-" + "q" * 24


def pin_vault() -> None:
    """This suite's scratch vault, whatever another suite in the same process set: a suite
    imported before this one (test_credential_doors) points the environment elsewhere."""
    os.environ["OBSERVATORY_VAULT_DIR"] = str(VAULT_DIR)
    vault.STORE, vault.LEAKS, vault.MOVES = VAULT_DIR, VAULT_DIR / "leaks.jsonl", VAULT_DIR / "movements.jsonl"


class Pinned(unittest.TestCase):
    def setUp(self):
        pin_vault()


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


def put(project: str, env: str, name: str, value: str = VALUE) -> None:
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "vault.py"), "put", project, env, name,
                        "--force"], input=value + "\n", capture_output=True, text=True, timeout=60,
                       env={**os.environ, "OBSERVATORY_VAULT_DIR": str(VAULT_DIR)})
    assert r.returncode == 0, r.stderr


def meta_of(project: str, env: str, name: str) -> dict:
    return json.loads((VAULT_DIR / project / env / f"{name}.meta.json").read_text())


def days_out(stamp: str) -> float:
    at = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    return (at - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 86400


class TheRule(unittest.TestCase):
    """`tools/key_expiry.py`: one lifetime, one spelling, one record shape."""

    def test_the_default_is_ninety_days_in_the_spelling_both_providers_take(self):
        at = key_expiry.expires_at(key_expiry.DEFAULT_DAYS, NOW)
        self.assertEqual(at, "2027-01-24T12:00:00Z")
        # OpenRouter refuses a timestamp without seconds; this one always carries them.
        self.assertRegex(at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIsNone(key_expiry.expires_at(None, NOW))

    def test_a_lifetime_the_doors_will_not_mint_is_refused(self):
        for bad in (0, -1, key_expiry.MAX_DAYS + 1, True, 1.5, "30"):
            with self.assertRaises(key_expiry.ExpiryError, msg=repr(bad)):
                key_expiry.check_days(bad)
        self.assertEqual(key_expiry.check_days(key_expiry.MAX_DAYS), key_expiry.MAX_DAYS)

    def test_a_provider_answer_is_read_not_guessed(self):
        self.assertEqual(key_expiry.from_seconds(86400, NOW), "2026-10-27T12:00:00Z")
        for none in (None, 0, -5, True, "86400"):
            self.assertIsNone(key_expiry.from_seconds(none, NOW))
        self.assertEqual(key_expiry.parse("2027-01-24"), "2027-01-24T00:00:00Z")
        self.assertEqual(key_expiry.parse("2027-01-24T12:00:00.123+00:00"), "2027-01-24T12:00:00Z")
        self.assertIsNone(key_expiry.parse("next spring"))

    def test_the_record_says_when_or_why_not_and_never_confuses_unknown_with_never(self):
        self.assertEqual(key_expiry.fields("2027-01-24T12:00:00Z"), {"expires_at": "2027-01-24T12:00:00Z"})
        self.assertEqual(key_expiry.fields(None, "asked"), {"expires_at": None, "expiry_reason": "asked"})
        meta = {"expiry_reason": "stale"}
        key_expiry.record(meta, "2027-01-24T12:00:00Z")
        self.assertNotIn("expiry_reason", meta, "an expiry drops a stale reason")
        self.assertEqual(key_expiry.describe(meta, NOW), "expires 2027-01-24")
        self.assertEqual(key_expiry.describe({"expires_at": "2026-01-01T00:00:00Z"}, NOW), "EXPIRED 2026-01-01")
        self.assertEqual(key_expiry.describe({"expires_at": None, "expiry_reason": "asked"}), "no expiry: asked")
        self.assertEqual(key_expiry.describe({}), "expiry not recorded")

    def test_the_two_flags_are_one_choice(self):
        p = argparse.ArgumentParser()
        key_expiry.add_flags(p, "--token-expire-days", "--no-token-expiry", "token")
        self.assertEqual(key_expiry.days_of(p.parse_args([])), key_expiry.DEFAULT_DAYS)
        self.assertEqual(key_expiry.days_of(p.parse_args(["--token-expire-days", "30"])), 30)
        self.assertIsNone(key_expiry.days_of(p.parse_args(["--no-token-expiry"])))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            p.parse_args(["--token-expire-days", "30", "--no-token-expiry"])


class TheVaultRecordsIt(Pinned):
    """`vault.record_expiry`: metadata only, journalled, shown where slots are listed."""

    def test_an_expiry_lands_in_the_slots_metadata_and_the_listings(self):
        put("expiry-demo", "prod", "API_TOKEN")
        line = vault.record_expiry("expiry-demo", "prod", "API_TOKEN", "2099-01-24T12:00:00Z", None,
                                   tool="test")
        self.assertEqual(line, "expires 2099-01-24")
        meta = meta_of("expiry-demo", "prod", "API_TOKEN")
        self.assertEqual(meta["expires_at"], "2099-01-24T12:00:00Z")
        self.assertNotIn(VALUE, json.dumps(meta))
        moves = [r for r in vault.movements("expiry-demo") if r["event"] == "expiry"]
        self.assertEqual(moves[-1]["expires_at"], "2099-01-24T12:00:00Z")
        self.assertNotIn(VALUE, json.dumps(moves))
        listed = subprocess.run([sys.executable, str(ROOT / "tools" / "vault.py"), "list", "expiry-demo"],
                                capture_output=True, text=True, timeout=60,
                                env={**os.environ, "OBSERVATORY_VAULT_DIR": str(VAULT_DIR)})
        self.assertIn("expires 2099-01-24", listed.stdout)
        named = subprocess.run([sys.executable, str(ROOT / "tools" / "use_secret.py"), "names", "expiry-demo"],
                               capture_output=True, text=True, timeout=60,
                               env={**os.environ, "OBSERVATORY_VAULT_DIR": str(VAULT_DIR)})
        self.assertIn("(expires 2099-01-24)", named.stdout, named.stdout + named.stderr)
        self.assertNotIn(VALUE, listed.stdout + named.stdout)

    def test_no_expiry_records_why_and_a_new_value_forgets_the_old_ones(self):
        put("expiry-demo", "local", "PLAIN")
        vault.record_expiry("expiry-demo", "local", "PLAIN", None, "minted without an expiry on request (x)")
        meta = meta_of("expiry-demo", "local", "PLAIN")
        self.assertIsNone(meta["expires_at"])
        self.assertEqual(meta["expiry_reason"], "minted without an expiry on request (x)")
        put("expiry-demo", "local", "PLAIN", "synthetic-second-value-" + "z" * 20)
        self.assertNotIn("expires_at", meta_of("expiry-demo", "local", "PLAIN"),
                         "a value put by hand is not described by the old value's expiry")

    def test_what_cannot_describe_a_slot_is_refused(self):
        with self.assertRaises(vault.VaultBoundaryError):
            vault.record_expiry("expiry-demo", "prod", "NOTHING_HERE", "2099-01-01T00:00:00Z")
        put("expiry-demo", "stage", "TOKEN")
        with self.assertRaises(vault.VaultBoundaryError):
            vault.record_expiry("expiry-demo", "stage", "TOKEN", "soon")

    def test_observatory_credentials_says_it_for_each_slot(self):
        import survey
        put("expiry-view", "prod", "MINTED")
        put("expiry-view", "prod", "BY_HAND")
        put("expiry-view", "prod", "FOREVER")
        vault.record_expiry("expiry-view", "prod", "MINTED", "2099-01-24T12:00:00Z")
        vault.record_expiry("expiry-view", "prod", "FOREVER", None, "the provider cannot set one")
        rows = {r["name"]: r for r in survey.credentials("expiry-view")["vault"]}
        self.assertEqual((rows["MINTED"]["expiry"], rows["MINTED"]["expiresAt"]), ("expires", "2099-01-24T12:00:00Z"))
        self.assertEqual((rows["FOREVER"]["expiry"], rows["FOREVER"]["expiresAt"], rows["FOREVER"]["expiryReason"]),
                         ("none", None, "the provider cannot set one"))
        self.assertEqual(rows["BY_HAND"]["expiry"], "not recorded")
        self.assertNotIn("expiresAt", rows["BY_HAND"])


# ─────────────────────────── Cloudflare ─────────────────────────────────────

def cloudflare():
    m = load("tools/cloudflare.py", "cf_expiry")
    m.ADMIN_STORE = pathlib.Path(tmpdir.mkdtemp()).resolve() / "cloudflare-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "acct", "admin-token\n")
    private_io.write(m.ADMIN_STORE / "acct.meta.json", json.dumps({"account_id": "a1", "account_name": "Acct"}))
    m._journal = lambda *a, **k: None
    return m


def cloudflare_fake(m, log, existing=None, answer="echo"):
    """`answer`: "echo" (Cloudflare's answer carries the expires_on it was sent), "absent"
    (the answer has no expires_on), or a timestamp the provider answers whatever was sent."""
    def said(payload):
        if answer == "absent":
            return {}
        return {"expires_on": (payload or {}).get("expires_on") if answer == "echo" else answer}

    def fake(path, token, payload=None, method=None):
        log.append((method or ("POST" if payload is not None else "GET"), path, payload))
        if "permission_groups" in path:
            return {"result": [{"id": "g1", "name": "Zone Read", "scopes": ["com.cloudflare.api.account.zone"]},
                               {"id": "g2", "name": "Analytics Read", "scopes": ["com.cloudflare.api.account.zone"]},
                               {"id": "g3", "name": "DNS Read", "scopes": ["com.cloudflare.api.account.zone"]},
                               {"id": "g4", "name": "DNS Write", "scopes": ["com.cloudflare.api.account.zone"]}]}
        if path.startswith("/zones?name=example.com&account.id=a1"):
            return {"result": [{"id": "z1", "name": "example.com"}]}
        if path.endswith("/tokens?per_page=50"):
            return {"result": [{"id": "t-old", "name": existing}] if existing else []}
        if path == "/accounts/a1/tokens" and payload is not None:
            return {"result": {"id": "t-new", "value": VALUE, **said(payload)}}
        if path == "/accounts/a1/tokens/t-old" and method == "PUT":
            return {"result": {"id": "t-old", **said(payload)}}
        if path == "/accounts/a1/tokens/t-old/value":
            return {"result": VALUE}
        if path.startswith("/zones/z1/dns_records"):
            return {"result": []}
        raise AssertionError(f"unexpected call {path}")
    m._request = fake


class CloudflareMintsWithAnExpiry(Pinned):

    def test_a_writer_is_created_with_expires_on_and_its_slot_records_it(self):
        m, log = cloudflare(), []
        cloudflare_fake(m, log)
        m.parse_vault_target = lambda t: tuple(t.split("/"))
        rc, out, err = quiet(m.cmd_issue_zone, "dns-edit", "example.com", "expiry-cf/prod/CF_DNS", None, wait=0)
        self.assertEqual(rc, 0, err)
        create = [p for meth, path, p in log if path == "/accounts/a1/tokens" and p]
        self.assertTrue(85 < days_out(create[0]["expires_on"]) <= 90, create[0])
        meta = meta_of("expiry-cf", "prod", "CF_DNS")
        self.assertEqual(meta["expires_at"], create[0]["expires_on"])
        self.assertIn(f"(expires {create[0]['expires_on'][:10]})", out)
        self.assertNotIn(VALUE, out + err + json.dumps(meta))

    def test_a_roll_renews_the_lifetime_before_the_value(self):
        m, log = cloudflare(), []
        cloudflare_fake(m, log, existing="observatory-dns-edit example.com (managed)")
        m.parse_vault_target = lambda t: tuple(t.split("/"))
        rc, _out, err = quiet(m.cmd_issue_zone, "dns-edit", "example.com", "expiry-cf/prod/CF_DNS_2", None,
                              wait=0, expire_days=30)
        self.assertEqual(rc, 0, err)
        rewrite = [p for meth, path, p in log if meth == "PUT" and path == "/accounts/a1/tokens/t-old"]
        self.assertTrue(25 < days_out(rewrite[0]["expires_on"]) <= 30, rewrite)
        self.assertEqual(meta_of("expiry-cf", "prod", "CF_DNS_2")["expires_at"], rewrite[0]["expires_on"])

    def test_no_expiry_is_asked_for_and_recorded_with_its_reason(self):
        m, log = cloudflare(), []
        cloudflare_fake(m, log)
        m.parse_vault_target = lambda t: tuple(t.split("/"))
        rc, _out, err = quiet(m.cmd_issue_zone, "dns-edit", "example.com", "expiry-cf/prod/CF_FOREVER", None,
                              wait=0, expire_days=None)
        self.assertEqual(rc, 0, err)
        create = [p for meth, path, p in log if path == "/accounts/a1/tokens" and p]
        self.assertNotIn("expires_on", create[0])
        meta = meta_of("expiry-cf", "prod", "CF_FOREVER")
        self.assertIsNone(meta["expires_at"])
        self.assertIn("--no-token-expiry", meta["expiry_reason"])

    def test_a_lifetime_out_of_range_is_refused_before_any_call(self):
        m, log = cloudflare(), []
        cloudflare_fake(m, log)
        m.parse_vault_target = lambda t: tuple(t.split("/"))
        rc, _out, err = quiet(m.cmd_issue_zone, "dns-edit", "example.com", "expiry-cf/prod/X", None,
                              wait=0, expire_days=400)
        self.assertEqual(rc, 2)
        self.assertEqual(log, [])
        self.assertIn("365", err)
        rc, _out, _err = quiet(m.cmd_issue, "analytics", None, None, 0)
        self.assertEqual(rc, 2)
        self.assertEqual(log, [])

    def test_a_plugin_token_records_its_expiry_and_a_rotation_keeps_its_lifetime(self):
        m, log = cloudflare(), []
        cloudflare_fake(m, log)
        d = pathlib.Path(tmpdir.mkdtemp()).resolve()
        m.token_dir = lambda: d
        m.zones_of = lambda value: [{"id": "z1", "name": "example.com", "account": "Acct"}]
        m.can_read_analytics = lambda value, ids: ""
        rc, out, err = quiet(m.cmd_issue, "analytics", None, None, 45)
        self.assertEqual(rc, 0, err)
        meta = json.loads((d / "acct.meta.json").read_text())
        self.assertEqual(meta["expire_days"], 45)
        self.assertTrue(40 < days_out(meta["expires_at"]) <= 45, meta)
        rc, listed, _err = quiet(m.cmd_list)
        self.assertIn(f"expires {meta['expires_at'][:10]}", listed)
        log.clear()
        cloudflare_fake(m, log, existing=m.PRESETS["analytics"]["name"])
        rc, _out, err = quiet(m.cmd_rotate, "acct", False)
        self.assertEqual(rc, 0, err)
        rewrite = [p for meth, path, p in log if meth == "PUT" and path == "/accounts/a1/tokens/t-old"]
        self.assertTrue(40 < days_out(rewrite[0]["expires_on"]) <= 45, rewrite)
        self.assertNotIn(VALUE, out + listed + json.dumps(meta))

    def test_what_is_recorded_is_cloudflares_answer_not_the_request(self):
        m, log = cloudflare(), []
        cloudflare_fake(m, log, answer="2027-03-01T00:00:00Z")
        m.parse_vault_target = lambda t: tuple(t.split("/"))
        rc, _out, err = quiet(m.cmd_issue_zone, "dns-edit", "example.com", "expiry-cf/prod/CF_ANSWERED", None,
                              wait=0)
        self.assertEqual(rc, 0, err)
        self.assertEqual(meta_of("expiry-cf", "prod", "CF_ANSWERED")["expires_at"], "2027-03-01T00:00:00Z")

    def test_an_answer_without_the_field_records_no_confirmed_expiry(self):
        m, log = cloudflare(), []
        cloudflare_fake(m, log, answer="absent")
        m.parse_vault_target = lambda t: tuple(t.split("/"))
        rc, _out, err = quiet(m.cmd_issue_zone, "dns-edit", "example.com", "expiry-cf/prod/CF_UNCONFIRMED",
                              None, wait=0)
        self.assertEqual(rc, 0, err)
        meta = meta_of("expiry-cf", "prod", "CF_UNCONFIRMED")
        self.assertIsNone(meta["expires_at"])
        self.assertEqual(meta["expiry_reason"], key_expiry.UNCONFIRMED)

    def test_a_roll_without_an_expiry_records_the_expiry_the_token_kept(self):
        m, log = cloudflare(), []
        cloudflare_fake(m, log, existing="observatory-dns-edit example.com (managed)",
                        answer="2026-12-01T00:00:00Z")
        m.parse_vault_target = lambda t: tuple(t.split("/"))
        rc, _out, err = quiet(m.cmd_issue_zone, "dns-edit", "example.com", "expiry-cf/prod/CF_KEPT", None,
                              wait=0, expire_days=None)
        self.assertEqual(rc, 0, err)
        rewrite = [p for meth, path, p in log if meth == "PUT" and path == "/accounts/a1/tokens/t-old"]
        self.assertNotIn("expires_on", rewrite[0])
        self.assertEqual(meta_of("expiry-cf", "prod", "CF_KEPT")["expires_at"], "2026-12-01T00:00:00Z",
                         "the token kept its old expiry, and that is what is recorded")

    def test_the_command_line_keeps_expire_days_for_the_bucket(self):
        src = (ROOT / "tools" / "cloudflare.py").read_text(encoding="utf-8")
        self.assertIn('key_expiry.add_flags(p_issue, "--token-expire-days", NO_EXPIRY_FLAG, "token")', src)
        self.assertIn('p_issue.add_argument("--expire-days"', src, "the R2 lifecycle flag is unchanged")


# ─────────────────────────── OpenRouter ─────────────────────────────────────

def openrouter(answer_expiry: str | None = None):
    m = load("tools/openrouter.py", "or_expiry")
    d = pathlib.Path(tmpdir.mkdtemp()).resolve()
    m.ADMIN_STORE = d / "openrouter-admin"
    m.ADMIN_STORE.mkdir(parents=True)
    private_io.write(m.ADMIN_STORE / "example", "prov\n")
    m.LEDGER = d / "ledger.json"
    m._journal = lambda *a, **k: None
    posted: list[dict] = []
    keys: dict[str, dict] = {}

    def fake(path, key, payload=None, method=None):
        if path.startswith("/keys?"):
            return {"data": list(keys.values())}
        if path == "/keys" and payload:
            posted.append(payload)
            h = f"h-{len(posted)}"
            row = {"hash": h, "label": f"sk-or-v1-{h}...x", "name": payload["name"]}
            if answer_expiry == "absent":
                pass
            elif answer_expiry == "null":
                row["expires_at"] = None
            else:
                row["expires_at"] = answer_expiry if answer_expiry is not None else payload.get("expires_at")
            keys[h] = row
            return {"data": row, "key": VALUE + str(len(posted))}
        if path.startswith("/keys/") and method == "DELETE":
            keys.pop(path.split("/")[2], None)
            return {}
        if path.startswith("/keys/") and method == "PATCH":
            keys[path.split("/")[2]]["name"] = payload["name"]
            return {"data": keys[path.split("/")[2]]}
        if path.startswith("/keys/"):
            return {"data": keys.get(path.split("/")[2])}
        if path.startswith("/activity"):
            return {"data": []}
        return {"data": {}}
    m._request = fake
    return m, posted


class OpenRouterMintsWithAnExpiry(Pinned):

    def test_a_key_is_created_with_expires_at_and_the_ledger_and_slot_record_it(self):
        m, posted = openrouter()
        r = m.issue_key("expiry-agent", 5.0, "example", "vault:expiry-or/prod/OPENROUTER_API_KEY", None)
        self.assertTrue(85 < days_out(posted[0]["expires_at"]) <= 90, posted)
        row = json.loads(m.LEDGER.read_text())["issued"]["expiry-agent"]
        self.assertEqual((row["expires_at"], row["expire_days"]), (posted[0]["expires_at"], 90))
        self.assertNotIn("expiry_reason", row)
        self.assertEqual(meta_of("expiry-or", "prod", "OPENROUTER_API_KEY")["expires_at"], posted[0]["expires_at"])
        self.assertEqual(r["expiry"], f"expires {posted[0]['expires_at'][:10]}")
        _rc, listed, _err = quiet(m.cmd_list)
        self.assertIn(f"expires {posted[0]['expires_at'][:10]}", listed)
        self.assertNotIn(VALUE, json.dumps(row) + listed + json.dumps(r))

    def test_the_providers_answer_is_what_is_recorded(self):
        m, posted = openrouter(answer_expiry="2027-02-01T00:00:00Z")
        m.deliver = lambda value, to, rotate=False: f"file {to}"
        m.issue_key("answered", 5.0, "example", "observatory", None, expire_days=30)
        row = json.loads(m.LEDGER.read_text())["issued"]["answered"]
        self.assertEqual(row["expires_at"], "2027-02-01T00:00:00Z")

    def test_a_provider_that_answers_null_or_nothing_is_not_recorded_as_the_request(self):
        for answer, reason in (("null", key_expiry.PROVIDER_SAID_NONE), ("absent", key_expiry.UNCONFIRMED)):
            m, posted = openrouter(answer_expiry=answer)
            m.deliver = lambda value, to, rotate=False: f"file {to}"
            m.issue_key("unanswered", 5.0, "example", "observatory", None)
            self.assertTrue(posted[0]["expires_at"], "an expiry was asked for")
            row = json.loads(m.LEDGER.read_text())["issued"]["unanswered"]
            self.assertIsNone(row["expires_at"], answer)
            self.assertEqual(row["expiry_reason"], reason)

    def test_no_expiry_is_asked_for_and_recorded_with_its_reason(self):
        m, posted = openrouter()
        m.deliver = lambda value, to, rotate=False: f"file {to}"
        rc, out, err = quiet(m.cmd_issue, "forever-agent", 5.0, "example", "observatory", None, "monthly", None)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("expires_at", posted[0])
        row = json.loads(m.LEDGER.read_text())["issued"]["forever-agent"]
        self.assertIsNone(row["expires_at"])
        self.assertIn("--no-expiry", row["expiry_reason"])
        self.assertIn("no expiry", out)

    def test_a_lifetime_out_of_range_mints_nothing(self):
        m, posted = openrouter()
        rc, _out, err = quiet(m.cmd_issue, "too-long", 5.0, "example", "observatory", None, "monthly", 500)
        self.assertEqual(rc, 2, err)
        self.assertEqual(posted, [])
        self.assertFalse(m.LEDGER.exists() and "too-long" in m.LEDGER.read_text())

    def test_a_rotation_mints_its_successor_with_the_keys_lifetime(self):
        m, posted = openrouter()
        m.deliver = lambda value, to, rotate=False: f"file {to}"
        m.issue_key("rolling", 5.0, "example", "observatory", None, expire_days=14)
        m.rotate_one("rolling")
        self.assertTrue(10 < days_out(posted[-1]["expires_at"]) <= 14, posted)
        row = json.loads(m.LEDGER.read_text())["issued"]["rolling"]
        self.assertEqual(row["expires_at"], posted[-1]["expires_at"])
        # A row from before the rule gets the default lifetime on its next rotation.
        doc = json.loads(m.LEDGER.read_text())
        for field in ("expire_days", "expires_at"):
            doc["issued"]["rolling"].pop(field)
        m.save_ledger(doc)
        m.rotate_one("rolling")
        self.assertTrue(85 < days_out(posted[-1]["expires_at"]) <= 90, posted)


# ─────────────────────────── Heroku ─────────────────────────────────────────

class HerokuMintsWithAnExpiry(Pinned):

    def setUp(self):
        pin_vault()
        import test_heroku_door as hd
        self.hd = hd
        self.m, self.fake, self.delivered = hd.setup()
        self.recorded: list[tuple] = []
        self.m.record_slot_expiry = lambda p, e, n, life: (self.recorded.append((p, e, n, life))
                                                           or key_expiry.describe(life))

    def test_the_default_asks_heroku_for_ninety_days_and_records_its_answer(self):
        rc, out, err = self.hd.run(self.m.cmd_issue, "read", "alpha/prod/K", None, None, None, wait=0)
        self.assertEqual(rc, 0, err)
        auth = next(iter(self.fake.auths.values()))
        self.assertEqual(auth["expires_in"], 90 * 86400)
        row = json.loads(self.m.LEDGER.read_text())["issued"]["alpha/prod/K"]
        self.assertTrue(89 < days_out(row["expires_at"]) <= 90, row)
        self.assertEqual(self.recorded[0][3]["expires_at"], row["expires_at"])
        self.assertIn(f"expires {row['expires_at'][:10]}", out)
        self.assertTrue(self.hd.no_value(out + err + json.dumps(row)))

    def test_a_never_expiring_authorization_is_replaced_not_regenerated(self):
        self.hd.run(self.m.cmd_issue, "read", "alpha/prod/K", None, None, None, None, wait=0)
        old = set(self.fake.auths)
        self.assertIsNone(next(iter(self.fake.auths.values()))["expires_in"])
        rc, out, err = self.hd.run(self.m.cmd_issue, "read", "alpha/prod/K", None, None, None, 30, wait=0)
        self.assertEqual(rc, 0, err)
        self.assertTrue(set(self.fake.auths).isdisjoint(old), "a new authorization, the old one deleted")
        self.assertEqual(next(iter(self.fake.auths.values()))["expires_in"], 30 * 86400)
        self.assertIn("was deleted", out)

    def test_another_lifetime_replaces_the_authorization_and_the_same_one_regenerates(self):
        self.hd.run(self.m.cmd_issue, "read", "alpha/prod/K", None, None, None, 30, wait=0)
        first = set(self.fake.auths)
        rc, _out, err = self.hd.run(self.m.cmd_issue, "read", "alpha/prod/K", None, None, None, 30, wait=0)
        self.assertEqual(rc, 0, err)
        self.assertEqual(set(self.fake.auths), first, "the same lifetime regenerates in place")
        rc, out, err = self.hd.run(self.m.cmd_issue, "read", "alpha/prod/K", None, None, None, 60, wait=0)
        self.assertEqual(rc, 0, err)
        self.assertTrue(set(self.fake.auths).isdisjoint(first), "another lifetime is a new authorization")
        self.assertEqual(next(iter(self.fake.auths.values()))["expires_in"], 60 * 86400)
        self.assertIn("was deleted", out)

    def test_rotating_a_row_from_before_the_rule_never_claims_no_expiry_was_asked(self):
        self.hd.run(self.m.cmd_issue, "read", "alpha/prod/K", None, None, None, None, wait=0)
        doc = json.loads(self.m.LEDGER.read_text())
        for field in ("expire_days", "expires_at", "expiry_reason"):
            doc["issued"]["alpha/prod/K"].pop(field, None)
        private_io.write(self.m.LEDGER, json.dumps(doc))
        rc, _out, err = self.hd.run(self.m.cmd_rotate, "alpha/prod/K", False, wait=0)
        self.assertEqual(rc, 0, err)
        row = json.loads(self.m.LEDGER.read_text())["issued"]["alpha/prod/K"]
        self.assertIsNone(row["expires_at"])
        self.assertEqual(row["expiry_reason"], self.m.OLD_ROW_NO_EXPIRY)
        self.assertNotIn("on request", row["expiry_reason"])

    def test_no_expiry_records_why(self):
        rc, _out, err = self.hd.run(self.m.cmd_issue, "read", "alpha/prod/K", None, None, None, None, wait=0)
        self.assertEqual(rc, 0, err)
        row = json.loads(self.m.LEDGER.read_text())["issued"]["alpha/prod/K"]
        self.assertIsNone(row["expires_at"])
        self.assertIn("--no-expiry", row["expiry_reason"])

    def test_out_of_range_is_refused_before_minting(self):
        rc, _out, err = self.hd.run(self.m.cmd_issue, "read", "alpha/prod/K", None, None, None, 3650, wait=0)
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.fake.auths, {})


# ─────────────────────────── the board ──────────────────────────────────────

class TheBoardSaysWhenAKeyExpires(unittest.TestCase):
    """`tools/expiry_findings.py`: within 14 days a warning, past it critical, no value."""

    def setUp(self):
        import expiry_findings
        self.f = expiry_findings
        base = pathlib.Path(tmpdir.mkdtemp()).resolve()
        self.vault, self.store = base / "projects", base / "store"
        self.tokens = self.store / "plugin-tokens.d"
        for d in (self.vault, self.tokens):
            d.mkdir(parents=True)

    def slot(self, slot: str, at: str | None, door: str = "heroku.py") -> None:
        path = self.vault / slot
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(VALUE + "\n")
        path.with_name(path.name + ".meta.json").write_text(json.dumps({"expires_at": at, "expiry_by": door}))

    def test_soon_is_a_warning_past_is_critical_and_far_is_nothing(self):
        self.slot("alpha/prod/SOON", "2026-11-05T00:00:00Z")
        self.slot("alpha/prod/GONE", "2026-10-20T00:00:00Z", door="cloudflare.py")
        self.slot("alpha/prod/FAR", "2027-01-24T00:00:00Z")
        self.slot("alpha/prod/FOREVER", None)
        (self.store / "openrouter-issued.json").write_text(json.dumps({"issued": {
            "agent-key": {"destination": "observatory", "expires_at": "2026-11-01T00:00:00Z"},
            "slot-key": {"destination": "vault:alpha/prod/SOON", "expires_at": "2026-11-05T00:00:00Z"}}}))
        (self.tokens / "acct.meta.json").write_text(json.dumps({"expires_at": "2026-10-25T00:00:00Z",
                                                                "kind": "issued",
                                                                "issued_by": "tools/cloudflare.py"}))
        (self.tokens / "other.meta.json").write_text(json.dumps({"expires_at": "2026-10-25T00:00:00Z"}))
        rows = {r["subject"]: r for r in self.f.from_store(self.vault, self.store, "2026-10-26")}
        self.assertEqual(set(rows), {"credential:vault/alpha/prod/SOON", "credential:vault/alpha/prod/GONE",
                                     "credential:openrouter/agent-key", "credential:cloudflare/acct"},
                         "a key delivered to a slot is one row, from the slot")
        soon = rows["credential:vault/alpha/prod/SOON"]
        self.assertEqual((soon["type"], soon["severity"]), ("credential.expiring", "warning"))
        self.assertIn("heroku.py", soon["action"])
        self.assertEqual(soon["title"], "alpha/prod/SOON expires on 2026-11-05")
        gone = rows["credential:vault/alpha/prod/GONE"]
        self.assertEqual((gone["type"], gone["severity"]), ("credential.expired", "critical"))
        self.assertIn("cloudflare.py", gone["action"])
        self.assertIn("openrouter.py\" rotate agent-key", rows["credential:openrouter/agent-key"]["action"])
        self.assertIn("rotate acct", rows["credential:cloudflare/acct"]["action"])
        self.assertNotIn(VALUE, json.dumps(rows))

    def test_an_openrouter_slot_names_its_key_in_the_rotate_command(self):
        self.slot("alpha/prod/OPENROUTER_API_KEY", "2026-11-01T00:00:00Z", door="openrouter.py")
        (self.store / "openrouter-issued.json").write_text(json.dumps({"issued": {
            "alpha-key": {"destination": "vault:alpha/prod/OPENROUTER_API_KEY"}}}))
        rows = self.f.from_store(self.vault, self.store, "2026-10-26")
        self.assertIn('openrouter.py" rotate alpha-key', rows[0]["action"])

    def test_a_key_that_expires_later_today_is_a_warning(self):
        self.slot("alpha/prod/TODAY", "2026-10-26T23:00:00Z")
        (row,) = self.f.from_store(self.vault, self.store, "2026-10-26")
        self.assertEqual((row["type"], row["severity"]), ("credential.expiring", "warning"))

    def test_a_damaged_ledger_never_stops_the_board(self):
        self.slot("alpha/prod/SOON", "2026-11-05T00:00:00Z", door="openrouter.py")
        do = self.store / "digitalocean-issued"
        do.mkdir()
        (do / "box.meta.json").write_text(json.dumps(["not", "a", "record"]))
        for issued in (["a", "b"], "text", 5, None):
            (self.store / "openrouter-issued.json").write_text(json.dumps({"issued": issued}))
            rows = self.f.from_store(self.vault, self.store, "2026-10-26")
            self.assertEqual([r["subject"] for r in rows], ["credential:vault/alpha/prod/SOON"], issued)

    def test_absent_stores_give_no_rows_and_the_board_runs_the_rule(self):
        self.assertEqual(self.f.from_store(self.vault / "none", self.store / "none", "2026-10-26"), [])
        src = (ROOT / "tools" / "build_findings.py").read_text(encoding="utf-8")
        self.assertIn("expiry_findings.from_store(", src)


# ─────────────────────────── DigitalOcean: cannot mint ──────────────────────

class DigitalOceanSaysItCannot(unittest.TestCase):

    def test_the_recipe_names_an_expiry_and_install_records_it_or_why_not(self):
        m = load("tools/digitalocean.py", "do_expiry")
        self.assertEqual(m._expiry_fields("2027-01-24"), {"expires_at": "2027-01-24T00:00:00Z"})
        none = m._expiry_fields(None)
        self.assertIsNone(none["expires_at"])
        self.assertIn("cannot", none["expiry_reason"])
        with self.assertRaises(m.DoorError):
            m._expiry_fields("in ninety days")
        src = (ROOT / "tools" / "digitalocean.py").read_text(encoding="utf-8")
        self.assertIn("expiration: {key_expiry.DEFAULT_DAYS} days", src)
        self.assertIn('" --expires-on YYYY-MM-DD"', src)


if __name__ == "__main__":
    unittest.main()
