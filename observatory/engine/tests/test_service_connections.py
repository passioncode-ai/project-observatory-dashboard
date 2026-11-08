#!/usr/bin/env python3
"""What Observatory uses, declared in its well-known document (`fabric-connections/0.1`, OBS-326).

Every case builds a temporary workspace and scratch folder and holds the declaration to the contract's own
schema, vendored at the pin (`fabric/interop-schemas/service-connections-extension.schema.json`). What
each class holds:

- Shape: the declaration is valid against the contract (declared, never reported), ids once.
- Configured: a provider whose integration is off is never declared, a leftover receipt included; one
  that is on but never ran reads `unknown`, never `ok`.
- State: a collector's own `degraded` rows make its connection `degraded`, an unreadable receipt too;
  a clean receipt is `ok`; `lastAt` is the collector's own time, never invented.
- Fabric's hub, the release feed and the usage analytics: each from the engine's own record.
- Nothing private: a reason never echoes a collector's text (a path, a name, a key).
- Stale (OBS-339): a receipt older than its collector's schedule allows reads `degraded` with the stale
  reason; the schedule is the tick's, read from `tools/tick.py`.
- Offline (OBS-340): `OBSERVATORY_OFFLINE=1` declares no provider, as `configuration.enabled` runs none.
- The hub's precedence (OBS-342): a refusal beside `hub-absent` is `degraded`; absent alone `unknown`.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import interop  # noqa: E402
import service_connections as sc  # noqa: E402

KINDS = ("service", "model", "connector", "store", "channel")
SCHEMA = interop.contract_validator("service-connections-extension.schema.json")

AT = "2026-11-05T10:00:00Z"
#: The time every case measures a receipt's age at: an hour after AT, so AT is fresh for every collector.
NOW = datetime.datetime(2026, 11, 5, 11, tzinfo=datetime.timezone.utc)


def stamp(when: datetime.datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve() / "workspace"
        (self.base / "store").mkdir(parents=True)
        (self.base / "workspace.json").write_text("{}", encoding="utf-8")
        # The collectors' output folder: passed in, as the server passes paths.SCRATCH.
        self.scratch = self.base / "scratch"
        self.scratch.mkdir()
        self.settings = {"integrations": {}, "features": {}}
        self.env = {}

    def receipt(self, name: str, doc) -> Path:
        path = self.scratch / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc, encoding="utf-8")
        return path

    def on(self, *names):
        for name in names:
            self.settings["integrations"][name] = True

    def declared(self, now=NOW):
        return sc.declared(self.base, self.scratch, self.settings, environ=self.env, now=now)

    def by_id(self):
        return {c["id"]: c for c in self.declared()}

    def view(self):
        """The declaration as a host reads it: declared when the key is there and the value is valid."""
        ext = sc.extension(self.base, self.scratch, self.settings, environ=self.env, now=NOW)
        value = ext.get(sc.KEY)
        errors = [] if value is None else [e.message for e in SCHEMA.iter_errors(value)] + sc.check(value)
        return {"declared": value is not None and not errors, "reported": bool(errors), "errors": errors,
                "connections": (value or {}).get("connections", [])}


class Shape(Base):
    def test_a_fresh_install_declares_what_it_always_uses_and_nothing_else(self):
        view = self.view()
        self.assertTrue(view["declared"], view["errors"])
        self.assertFalse(view["reported"])
        ids = {c["id"] for c in view["connections"]}
        self.assertEqual(ids, {"fabric-hub", "release-feed"}, "no provider that is not configured")
        for c in view["connections"]:
            self.assertEqual(c["state"], "unknown", "never used on this install: unknown, never ok")
            self.assertNotIn("lastAt", c)

    def test_every_connection_has_the_contract_members_only(self):
        self.on(*sc.INTEGRATIONS)
        for c in self.declared():
            self.assertLessEqual(set(c), {"id", "kind", "target", "capability", "state", "label", "purpose",
                                          "reason", "lastAt"})
            self.assertIn(c["kind"], KINDS)
        self.assertTrue(self.view()["declared"])

    def test_the_hub_is_a_service_by_its_product_id_with_the_capability_it_calls(self):
        hub = self.by_id()["fabric-hub"]
        self.assertEqual((hub["kind"], hub["target"], hub["capability"]), ("service", "fabric", "project.resolve"))

    def test_providers_are_connectors(self):
        self.on("github", "cloudflare", "openrouter")
        found = self.by_id()
        for ident in ("github", "cloudflare", "openrouter"):
            self.assertEqual(found[ident]["kind"], "connector")


class Configured(Base):
    def test_an_integration_that_is_off_is_never_declared_even_with_a_leftover_receipt(self):
        self.receipt("heroku.json", {"scanned_at": AT, "degraded": []})
        self.assertNotIn("heroku", self.by_id())
        self.settings["integrations"]["heroku"] = False
        self.assertNotIn("heroku", self.by_id())

    def test_on_but_never_run_is_unknown(self):
        self.on("bitbucket")
        c = self.by_id()["bitbucket"]
        self.assertEqual(c["state"], "unknown")
        self.assertNotIn("lastAt", c)


class State(Base):
    def test_a_clean_receipt_is_ok_with_the_collectors_own_time(self):
        self.on("digitalocean")
        self.receipt("digitalocean.json", {"scanned_at": AT, "degraded": []})
        c = self.by_id()["digitalocean"]
        self.assertEqual((c["state"], c["lastAt"]), ("ok", AT))

    def test_the_collectors_degraded_rows_make_the_connection_degraded(self):
        self.on("google")
        self.receipt("google.json", {"scanned_at": AT, "degraded": [
            {"source": "/srv/someone/private-project", "reason": "token ghp_" + "A" * 36 + " refused"}]})
        c = self.by_id()["google"]
        self.assertEqual(c["state"], "degraded")
        self.assertNotIn("someone", json.dumps(c))
        self.assertNotIn("ghp_", json.dumps(c), "a reason never echoes the collector's text")
        self.assertTrue(self.view()["declared"], "and the declaration stays valid")

    def test_an_unreadable_receipt_is_degraded(self):
        self.on("heroku")
        self.receipt("heroku.json", "{not json")
        self.assertEqual(self.by_id()["heroku"]["state"], "degraded")

    def test_several_receipts_take_the_worst_state_and_the_latest_time(self):
        self.on("cloudflare")
        self.receipt("cloudflare_zones.json", {"scanned_at": "2026-11-05T09:00:00Z", "degraded": []})
        self.receipt("cloudflare_account.json", {"scanned_at": AT, "degraded": [{"source": "a", "reason": "b"}]})
        c = self.by_id()["cloudflare"]
        self.assertEqual((c["state"], c["lastAt"]), ("degraded", AT))

    def test_a_receipt_without_a_time_takes_the_time_the_engine_wrote_it(self):
        self.on("github")
        path = self.receipt("gh/_degraded.json", [])
        written = int(NOW.timestamp()) - 3600
        os.utime(path, (written, written))
        c = self.by_id()["github"]
        self.assertEqual(c["state"], "ok")
        self.assertEqual(c["lastAt"], datetime.datetime.fromtimestamp(written, datetime.timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(sc.check({"connections": [c]}), [])


class Hub(Base):
    def test_a_hub_that_answers_is_ok(self):
        self.receipt("fabric-attribution.json", {"checked_at": AT, "hub": {"origin": "x"}, "degraded": []})
        hub = self.by_id()["fabric-hub"]
        self.assertEqual((hub["state"], hub["lastAt"]), ("ok", AT))

    def test_a_refusal_beside_hub_absent_is_degraded_and_absent_alone_unknown(self):
        # OBS-342: the precedence is explicit. A refusal is a fault; no hub alone is not.
        for reasons, state in ((["hub-absent"], "unknown"), (["hub-absent", "access-refused"], "degraded"),
                               (["access-refused", "hub-absent"], "degraded"), (["hub-absent", "hub-dead"], "down"),
                               (["hub-absent", "hub-absent"], "unknown")):
            with self.subTest(reasons=reasons):
                self.receipt("fabric-attribution.json", {"checked_at": AT, "degraded": [
                    {"source": "fabric", "reason": r} for r in reasons]})
                hub = self.by_id()["fabric-hub"]
                self.assertEqual((hub["state"], hub["lastAt"]), (state, AT))
                if state == "degraded":
                    self.assertEqual(hub["reason"], sc.REASONS["degraded"])

    def test_a_dead_hub_is_down_no_hub_is_unknown_and_a_refusal_degraded(self):
        # No hub on this machine (`hub-absent`) means Fabric is not installed, not an outage.
        for reason, state in (("hub-absent", "unknown"), ("hub-dead", "down"), ("access-refused", "degraded")):
            with self.subTest(reason=reason):
                self.receipt("fabric-attribution.json", {"checked_at": AT, "degraded": [
                    {"source": "fabric", "reason": reason}]})
                self.assertEqual(self.by_id()["fabric-hub"]["state"], state)


class Stale(Base):
    """OBS-339: a receipt older than its collector's schedule allows is `degraded`, never a lasting `ok`."""

    def at(self, hours_ago: float) -> str:
        return stamp(NOW - datetime.timedelta(hours=hours_ago))

    def test_the_threshold_is_three_intervals_never_under_six_hours(self):
        self.assertEqual(sc.STALE_FACTOR, 3)
        self.assertEqual(sc.stale_after("gh/_degraded.json"), datetime.timedelta(hours=6), "every tick: the floor")
        self.assertEqual(sc.stale_after("bitbucket.json"), datetime.timedelta(hours=18))
        self.assertEqual(sc.stale_after("heroku.json"), datetime.timedelta(hours=72))

    def test_a_fresh_receipt_is_ok_and_an_old_one_degraded_with_the_stale_reason(self):
        self.on("github", "heroku")
        for name, ident, fresh, old in (("gh/_degraded.json", "github", 5.5, 6.5), ("heroku.json", "heroku", 71, 73)):
            with self.subTest(ident=ident):
                self.receipt(name, {"scanned_at": self.at(fresh), "degraded": []})
                c = self.by_id()[ident]
                self.assertEqual((c["state"], c["lastAt"]), ("ok", self.at(fresh)))
                self.assertNotIn("reason", c)
                self.receipt(name, {"scanned_at": self.at(old), "degraded": []})
                c = self.by_id()[ident]
                self.assertEqual((c["state"], c["lastAt"]), ("degraded", self.at(old)), "the old time is kept")
                self.assertEqual(c["reason"], sc.REASONS["stale"])
        self.assertTrue(self.view()["declared"])

    def test_the_same_receipt_turns_stale_as_the_clock_moves(self):
        self.on("google")
        self.receipt("google.json", {"scanned_at": AT, "degraded": []})
        self.assertEqual(self.declared(NOW)[-1]["state"], "ok")
        later = NOW + sc.stale_after("google.json")  # 36 hours: scan_google's own 12-hour gate, three times
        self.assertEqual(self.declared(later)[-1]["state"], "degraded")

    def test_a_receipt_without_its_own_time_is_aged_by_when_it_was_written(self):
        self.on("github")
        path = self.receipt("gh/_degraded.json", [])
        for hours, state in ((5, "ok"), (7, "degraded")):
            with self.subTest(hours=hours):
                written = int(NOW.timestamp()) - hours * 3600
                os.utime(path, (written, written))
                self.assertEqual(self.by_id()["github"]["state"], state)

    def test_a_fresh_and_a_stale_receipt_combine_to_degraded(self):
        self.on("cloudflare")
        self.receipt("cloudflare_zones.json", {"scanned_at": self.at(0.5), "degraded": []})
        self.receipt("cloudflare_account.json", {"scanned_at": self.at(80), "degraded": []})
        c = self.by_id()["cloudflare"]
        self.assertEqual((c["state"], c["lastAt"], c["reason"]), ("degraded", self.at(0.5), sc.REASONS["stale"]))

    def test_an_old_receipt_with_degraded_rows_names_the_staleness(self):
        self.on("openrouter")
        self.receipt("openrouter.json", {"scanned_at": self.at(100), "degraded": [{"source": "a", "reason": "b"}]})
        c = self.by_id()["openrouter"]
        self.assertEqual((c["state"], c["reason"]), ("degraded", sc.REASONS["stale"]))

    def test_the_hub_old_receipt(self):
        old = self.at(7)
        for rows, state, reason in (([], "degraded", "stale"), ([{"reason": "hub-absent"}], "degraded", "stale"),
                                    ([{"reason": "hub-dead"}], "down", "down")):
            with self.subTest(rows=rows):
                self.receipt("fabric-attribution.json", {"checked_at": old, "degraded": rows})
                hub = self.by_id()["fabric-hub"]
                self.assertEqual((hub["state"], hub["reason"], hub["lastAt"]), (state, sc.REASONS[reason], old))

    def test_every_receipt_has_the_interval_the_tick_gives_its_collector(self):
        # The intervals are the tick's schedule (tools/tick.py), not a second opinion: read them from the source.
        tick = (ROOT / "tools" / "tick.py").read_text(encoding="utf-8")
        stale_hours = float(re.search(r"^STALE_HOURS = (\d+(?:\.\d+)?)$", tick, re.M).group(1))
        receipts = {name for _, _, _, names, _ in sc.PROVIDERS for name in names} | {"fabric-attribution.json"}
        self.assertEqual(set(sc.INTERVAL_HOURS), receipts, "every receipt has an interval, and only those")
        for name, hours in sc.INTERVAL_HOURS.items():
            with self.subTest(receipt=name):
                written = name.split("/")[0]
                self.assertIn(f'SCRATCH / "{written}"', tick, "the tick runs the collector that writes it")
                gate = re.search(r'stale\(SCRATCH / "' + re.escape(written) + r'", (\w+|\d+)\)', tick)
                if name == "bitbucket.json":
                    gate = re.search(r'stale\(SCRATCH / "remotes\.json", (\w+)\)', tick)
                    self.assertIn('SCRATCH / "bitbucket.json"', tick[gate.end():gate.end() + 400])
                value = sc.TICK_HOURS if gate is None else (
                    stale_hours if gate.group(1) == "STALE_HOURS" else float(gate.group(1)))
                # A collector with an age gate of its own writes nothing inside it, so its healthy receipt can
                # be that old (scan_google: 12 hours): the interval is the longer of the two.
                runs = re.search(r'collectors/(\w+)\.py"[^\n]*SCRATCH / "' + re.escape(written) + '"', tick)
                if runs:
                    source = (ROOT / "collectors" / f"{runs.group(1)}.py").read_text(encoding="utf-8")
                    own = re.search(r"^MAX_AGE_HOURS = (\d+(?:\.\d+)?)$", source, re.M)
                    if own:
                        value = max(value, float(own.group(1)))
                self.assertEqual(hours, value)

    def test_a_google_receipt_inside_its_own_twelve_hours_is_not_stale(self):
        # The #442 review: scan_google skips a scan younger than 12 hours, so a 9-hour-old receipt is healthy.
        self.assertGreaterEqual(sc.INTERVAL_HOURS["google.json"], 12)
        self.assertGreater(sc.stale_after("google.json"), datetime.timedelta(hours=12.5))

    def test_the_tick_interval_is_the_schedulers_default(self):
        launchd = (ROOT / "tools" / "install_launchd.py").read_text(encoding="utf-8")
        self.assertRegex(launchd, r'"--interval", type=int, default=%d\b' % int(sc.TICK_HOURS * 3600))

    def test_the_clock_bucket_moves_every_ten_minutes(self):
        self.assertEqual(sc.clock_bucket(6000.0), sc.clock_bucket(6599.0))
        self.assertNotEqual(sc.clock_bucket(6599.0), sc.clock_bucket(6600.0))


class Offline(Base):
    """OBS-340: the declaration asks `configuration.enabled`, so `OBSERVATORY_OFFLINE=1` declares no provider."""

    def test_offline_declares_no_provider_and_online_does(self):
        self.on("github", "heroku")
        self.receipt("heroku.json", {"scanned_at": AT, "degraded": []})
        self.assertLessEqual({"github", "heroku"}, set(self.by_id()))
        self.env = {"OBSERVATORY_OFFLINE": "1"}
        found = self.by_id()
        self.assertFalse({"github", "heroku"} & set(found), "offline: no collector runs, none is declared")
        self.assertLessEqual({"fabric-hub", "release-feed"}, set(found))
        self.env = {"OBSERVATORY_OFFLINE": "0"}
        self.assertIn("heroku", self.by_id(), "only the value 1 is offline, as for the collectors")

    def test_the_same_rule_as_the_collectors(self):
        import configuration
        self.on("github")
        for env in ({}, {"OBSERVATORY_OFFLINE": "1"}):
            with self.subTest(env=env):
                self.env = env
                self.assertEqual("github" in self.by_id(),
                                 configuration.enabled("github", settings=self.settings, environ=env))


class ReleaseFeed(Base):
    def check(self, record):
        (self.base / "store" / "update-check.json").write_text(json.dumps(record), encoding="utf-8")

    def test_the_last_check_decides(self):
        for result, state in (("up-to-date", "ok"), ("update-available", "ok"), ("undetermined", "degraded")):
            with self.subTest(result=result):
                self.check({"check": {"at": AT, "result": result}})
                feed = self.by_id()["release-feed"]
                self.assertEqual((feed["state"], feed["lastAt"]), (state, AT))
                self.assertEqual(feed["kind"], "connector")


class Analytics(Base):
    def test_declared_only_while_collecting_and_its_state_from_the_last_send(self):
        self.assertNotIn("usage-analytics", self.by_id(), "no App-Key: nothing is collected or sent")
        self.env = {"OBSERVATORY_ANALYTICS_APP_KEY": "A-SH-0000000000"}
        self.assertEqual(self.by_id()["usage-analytics"]["state"], "unknown", "nothing sent yet")
        import usage_analytics as UA
        UA.note_send(self.base, ok=True, at=datetime.datetime(2026, 11, 5, 10, tzinfo=datetime.timezone.utc))
        c = self.by_id()["usage-analytics"]
        self.assertEqual((c["state"], c["lastAt"]), ("ok", AT))
        UA.note_send(self.base, ok=False, at=datetime.datetime(2026, 11, 5, 11, tzinfo=datetime.timezone.utc))
        self.assertEqual(self.by_id()["usage-analytics"]["state"], "degraded")
        self.env["DO_NOT_TRACK"] = "1"
        self.assertNotIn("usage-analytics", self.by_id(), "off: not used")


class Check(Base):
    def test_the_contracts_rules_refuse_what_a_host_would_hide(self):
        good = {"id": "x", "kind": "connector", "target": "x", "state": "ok"}
        self.assertEqual(sc.check({"connections": [good]}), [])
        for bad in ({**good, "target": "a b"}, {**good, "kind": "robot"}, {**good, "capability": "project.resolve"},
                    {**good, "kind": "service", "target": "Not_A_Service"}, {**good, "extra": "x"},
                    {**good, "purpose": "see https://example.invalid/page"}, {**good, "lastAt": "2026-11-05"}):
            with self.subTest(bad=bad):
                self.assertTrue(sc.check({"connections": [bad]}))
        self.assertTrue(sc.check({"connections": [good, good]}), "an id named twice")


class Fallback(Base):
    def test_a_declaration_the_kit_refuses_is_named_never_published(self):
        out = sc.extension(self.base, self.scratch, self.settings, environ=self.env,
                           declared=lambda *a, **k: [{"id": "x", "kind": "connector", "target": "a b", "state": "ok"}])
        self.assertEqual(out, {})
        self.assertTrue(sc.LAST_PROBLEM)


if __name__ == "__main__":
    unittest.main()
