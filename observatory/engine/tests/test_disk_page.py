#!/usr/bin/env python3
"""OBS-562, CP-16: disk and cleanup as their own page.

The Disk page answers how much space is free, what the disk guard and the cleanup's auto tier removed and keep, and
how they are set. Every setting the configuration door declares for the guard and the cleanup is shown with its value,
its default and where the value comes from. A change is made from the page only when the page is served by the local
control server (the key server): the control posts to it, the server asks the configuration door for a plan, the page
shows it, and the person applies it and can undo it. Opened as a file, the controls are disabled and say why.

Also here: the Machine page keeps one line about the disk with a link, the navigation lists the page after Machine,
and the server's route answers plan, apply and undo, refuses an unknown key and refuses a caller without the token.
"""
from __future__ import annotations

import html
import importlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(ROOT / "tests"))

import disk_page  # noqa: E402
import machine_page  # noqa: E402
import shell  # noqa: E402
from i18n import Translator  # noqa: E402

#: The workspace the page was built for is named nowhere on it: no command line a person would copy.
COMMAND = re.compile(r"project-observatory full|--apply|OBSERVATORY_HOME")


def shown(markup: str) -> str:
    """What a reader sees: the text, without the marks' ids and arguments."""
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def setting(key, value, default, *, set_=False, file="config/cleanup.json", kind="int", minimum=0, maximum=3650,
            person="lower"):
    row = {"key": key, "type": kind, "default": default, "file": file, "description": "", "value": value,
           "set": set_, "gate": {"person": person}}
    if kind in ("int", "number"):
        row.update(minimum=minimum, maximum=maximum)
    return row


SETTINGS = {"settings": [
    setting("features.auto_cleanup", True, False, set_=True, file="config/settings.json", kind="bool", person="on"),
    setting("guard.levels.low.free_gb", 25, 25, kind="number", maximum=10000, person="raise"),
    setting("guard.temp_days.normal", 3, 3),
    setting("cleanup.auto.merged_branch_idle_days", 14, 7, set_=True),
], "degraded": []}

GUARD = {"at": "2026-11-10T08:00:00Z", "level": "low", "freeGbBefore": 20.1, "freeGbAfter": 31.4, "freedGb": 11.3,
         "results": {"removed": 42}, "planned": {"temp": 30, "build-output": 12},
         "kept": [{"target": "/data/caches/svc-jobs",
                   "reason": "the folder carries .observatory-keep: an agent service keeps live state here"}],
         "largeFiles": {"complete": True, "measured_at": "2026-11-10T08:00:00Z",
                        "files": [{"path": "/data/alpha-web/big.img", "gb": 8.0, "modified": "2026-11-01"}]},
         "memory": {"simulators": [{"name": "Phone A", "rss_mb": 2048, "command": "xcrun simctl shutdown 'Phone A'"}]}}

CLEANUP = {"plannedAt": "2026-11-10T08:00:00Z", "autoEnabled": True, "level": "low",
           "counts": {"branch-merged": 3, "worktree-dirty": 1},
           "journal": [{"at": "2026-11-09T10:00:00Z", "class": "branch-merged", "target": "/data/alpha-web#old",
                        "result": "removed"}],
           "guardRuns": [{"at": "2026-11-10T08:00:00Z", "level": "low", "removed": 42, "freed_mb": 11571}],
           "guardTotals": {"runs": 1, "removed": 42, "freedMb": 11571}}


def machine(**over) -> dict:
    m = {"measuredAt": "2026-11-10T08:05:00Z", "degraded": [],
         "disk": {"volume": {"free_gb": 31.4, "free_percent": 12.5, "total_gb": 250}},
         "guard": GUARD, "cleanup": CLEANUP, "diskSettings": SETTINGS}
    m.update(over)
    return m


def both(m: dict) -> tuple[str, str]:
    return (disk_page.disk_html({"machine": m}, Translator("en")),
            disk_page.disk_html({"machine": m}, Translator("ru")))


def relocalizes(test: unittest.TestCase, en: str, ru: str) -> None:
    import relocalize
    for built, reader in (("en", "ru"), ("ru", "en")):
        got = relocalize.relocalize({"en": en, "ru": ru}[built], reader)
        want = {"en": en, "ru": ru}[reader]
        test.assertTrue(got == want, f"{built}->{reader}: " + relocalize.first_difference(got, want))


class Page(unittest.TestCase):
    def test_a_guard_that_ran_shows_free_space_its_pass_what_it_keeps_and_the_journals(self):
        en, ru = both(machine())
        text = shown(en)
        for want in ("31.4 GB", "12.5% of 250 GB", "Disk guard ran", "20.1", "31.4", "Largest files", "big.img",
                     "Kept by the guard", "svc-jobs", "Guard passes in 7 days", "11.3 GB",
                     "Cleaned in the last 7 days", "alpha-web#old", "Auto cleanup", "Settings"):
            self.assertIn(want, text)
        self.assertIn("Самые большие файлы", shown(ru))
        self.assertIsNone(COMMAND.search(en), "no command line on the page")
        self.assertIsNone(COMMAND.search(ru))
        self.assertNotIn("Running simulators", text, "simulators hold memory: they stay on the Machine page")
        relocalizes(self, en, ru)

    def test_what_auto_cleanup_removes_and_keeps_with_its_idle_days(self):
        en, _ = both(machine())
        text = shown(en)
        # Removed automatically: each auto class with the idle days the setting holds now (14, not the default 7).
        self.assertRegex(text, r"merged branches\s+auto\s+3\s+14 days")
        # Kept: the manual tier holds the only copy of someone's work.
        self.assertRegex(text, r"uncommitted worktrees\s+kept\s+1")

    def test_a_guard_that_never_ran_says_so_without_a_command(self):
        en, ru = both(machine(guard=None, cleanup=None))
        self.assertIn("The disk guard has not run yet.", shown(en))
        self.assertIsNone(COMMAND.search(en))
        relocalizes(self, en, ru)

    def test_auto_cleanup_off_says_so_and_offers_to_turn_it_on(self):
        rows = [dict(r, value=False, set=False) if r["key"] == "features.auto_cleanup" else r
                for r in SETTINGS["settings"]]
        en, ru = both(machine(cleanup={**CLEANUP, "autoEnabled": False}, diskSettings={"settings": rows,
                                                                                       "degraded": []}))
        text = shown(en)
        self.assertIn("Auto cleanup is off", text)
        button = re.search(r'<button[^>]*data-cfg-key="features.auto_cleanup"[^>]*>', en).group(0)
        self.assertIn('data-cfg-value="true"', button)
        relocalizes(self, en, ru)

    def test_a_degraded_source_and_an_unreadable_settings_file_are_named(self):
        rows = [dict(r, value=None) if r["file"] == "config/cleanup.json" else r for r in SETTINGS["settings"]]
        m = machine(guard=None, degraded=[{"source": "disk-guard.json", "code": "unreadable", "error": "ValueError",
                                           "reason": "unreadable: ValueError"},
                                          {"source": "machine.json", "code": "not-surveyed",
                                           "reason": "not surveyed yet"}],
                    diskSettings={"settings": rows, "degraded": [{"source": "config/cleanup.json",
                                                                 "code": "unreadable", "reason": "bad json"}]})
        en, ru = both(m)
        text = shown(en)
        self.assertIn("Not measured", text)
        self.assertIn("disk-guard.json", text)
        self.assertIn("config/cleanup.json", text)
        self.assertIn("unreadable", text)
        self.assertIsNone(COMMAND.search(en))
        relocalizes(self, en, ru)

    def test_each_setting_shows_its_value_its_default_and_its_source(self):
        en, _ = both(machine())
        rows = {m.group(1): shown(m.group(0)) for m in
                re.finditer(r'<tr data-cfg-row="([^"]+)">.*?</tr>', en, re.S)}
        self.assertEqual(set(rows), {r["key"] for r in SETTINGS["settings"]})
        self.assertRegex(rows["features.auto_cleanup"], r"on\s+off\s+set in config/settings.json")
        self.assertRegex(rows["guard.levels.low.free_gb"], r"25\s+25\s+default")
        self.assertRegex(rows["cleanup.auto.merged_branch_idle_days"], r"14\s+7\s+set in config/cleanup.json")

    def test_controls_are_disabled_until_the_server_serves_the_page_and_name_the_agent_tool(self):
        en, _ = both(machine())
        controls = re.findall(r"<(?:button|input)[^>]*data-cfg-[a-z]+=[^>]*>", en)
        self.assertTrue(controls)
        for control in controls:
            self.assertIn(" disabled", control)
            self.assertIn("observatory_configure", control, "the tooltip names the agent's tool")
        self.assertIn('id="disk-live"', en)
        self.assertIn("opened as a file", shown(en))
        number = re.search(r'<input[^>]*data-cfg-input="cleanup.auto.merged_branch_idle_days"[^>]*>', en).group(0)
        for attr in ('type="number"', 'min="0"', 'max="3650"', 'value="14"'):
            self.assertIn(attr, number)
        self.assertIn('id="disk-plan"', en)

    def test_every_guard_and_cleanup_setting_the_door_declares_has_a_label(self):
        import configuration_door
        keys = {k for k in configuration_door.catalogue() if configuration_door.in_disk_scope(k)}
        self.assertIn("features.auto_cleanup", keys)
        self.assertTrue(any(k.startswith("guard.") for k in keys))
        self.assertTrue(any(k.startswith("cleanup.auto.") for k in keys))
        self.assertEqual(sorted(keys - set(disk_page.SETTING_LABELS)), [])
        self.assertFalse(configuration_door.in_disk_scope("integrations.vulnerabilities"))

    def test_settings_of_keeps_only_the_disk_keys(self):
        listing = {"settings": [{"key": "features.auto_cleanup"}, {"key": "interface.locale"},
                                {"key": "guard.trash_days.low"}, {"key": "cleanup.auto.pushed_branch_idle_days"}],
                   "degraded": [{"source": "config/cleanup.json"}]}
        got = disk_page.settings_of(listing)
        self.assertEqual([r["key"] for r in got["settings"]],
                         ["features.auto_cleanup", "guard.trash_days.low", "cleanup.auto.pushed_branch_idle_days"])
        self.assertEqual(got["degraded"], listing["degraded"])


class Shell(unittest.TestCase):
    def test_the_disk_page_follows_installed_tools_in_the_system_group(self):
        names = [p[0] for p in shell.PAGES]
        self.assertEqual(names.index("disk"), names.index("tools") + 1)
        self.assertEqual(dict(shell.PAGES and [(p[0], p[1]) for p in shell.PAGES])["disk"], "Disk")
        system = dict((k, n) for k, _l, n in shell.NAV_GROUPS)["system"]
        self.assertEqual(system[system.index("tools") + 1], "disk")
        self.assertIn("disk", shell.QUESTIONS)

    def test_the_page_is_built_with_its_section_and_its_data(self):
        payload = {"machine": machine(), "agents": {"x": 1}}
        sliced = shell.slice_for("disk", payload)
        self.assertEqual(sliced["machine"], payload["machine"])
        self.assertIsNone(sliced["agents"])
        self.assertIsNone(shell.slice_for("projects", payload)["machine"])
        import build_dashboard
        page = shell.page_html(shell.split_template(build_dashboard.template_for("en"))[0], "disk", payload)
        self.assertIn('<section id="disk"', page)
        self.assertIn('href="disk.html" aria-current="page"', page)
        self.assertNotIn("__DISK__", build_dashboard.template_for("en").replace("__DISK__", "", 1))


class MachinePage(unittest.TestCase):
    def test_the_machine_page_keeps_one_line_and_a_link_and_keeps_the_simulators(self):
        m = machine()
        en = machine_page.machine_html({"machine": m}, Translator("en"))
        ru = machine_page.machine_html({"machine": m}, Translator("ru"))
        text = shown(en)
        for gone in ("Kept by the guard", "Largest files", "Cleanup plan", "Cleaned in the last 7 days"):
            self.assertNotIn(gone, text)
        self.assertNotIn("disk-guard --apply", en)
        self.assertIn('href="disk.html"', en)
        self.assertIn("Running simulators", text)
        relocalizes(self, en, ru)


# --- the server's route -------------------------------------------------------------------------------------------

TOKEN = "-".join(("a", "token", "only", "this", "suite", "knows"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Route(unittest.TestCase):
    """POST /api/configure on the key server: the same door as `observatory_configure`, as the person."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.home = base / "home"
        self.env = patch.dict(os.environ, {"OBSERVATORY_HOME": str(self.home), "HOME": str(base / "user"),
                                           "USERPROFILE": str(base / "user")}, clear=False)
        self.env.start()
        import workspace
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        import configuration_door
        importlib.reload(configuration_door)
        sys.path.insert(0, str(ROOT / "tools"))
        import keyserver
        self.ks = keyserver
        self.audit = keyserver.AUDIT
        keyserver.AUDIT = base / "keyserver.jsonl"
        self.port = _free_port()
        self.srv = keyserver.Server(("127.0.0.1", self.port), keyserver.Handler, TOKEN)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        time.sleep(0.1)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.ks.AUDIT = self.audit
        self.env.stop()
        self.tmp.cleanup()

    def post(self, body: dict, *, token: str | None = TOKEN) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json", "X-Observatory-Caller": "page:disk",
                   "Origin": f"http://127.0.0.1:{self.port}"}
        if token:
            headers["X-Observatory-Token"] = token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/configure", data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read().decode())

    def journal(self) -> list[dict]:
        file = self.home / "store" / "logs" / "configure.jsonl"
        return [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines()] if file.exists() else []

    def stored(self) -> dict:
        return json.loads((self.home / "config" / "cleanup.json").read_text(encoding="utf-8"))

    def test_plan_then_apply_then_undo_as_the_person(self):
        before = self.stored()["auto"]["merged_branch_idle_days"]
        code, plan = self.post({"op": "plan", "changes": {"cleanup.auto.merged_branch_idle_days": 3}})
        self.assertEqual(code, 200, plan)
        self.assertRegex(plan["digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(plan["needsPerson"], ["cleanup.auto.merged_branch_idle_days"])
        self.assertEqual(plan["changes"][0]["after"], 3)
        self.assertNotIn("apply", plan, "no command line travels to the page")
        self.assertEqual(self.journal(), [], "a plan writes nothing")
        code, done = self.post({"op": "apply", "digest": plan["digest"]})
        self.assertEqual(code, 200, done)
        self.assertEqual(done["status"], "applied")
        self.assertNotIn("undo", done)
        self.assertEqual(self.stored()["auto"]["merged_branch_idle_days"], 3)
        line = self.journal()[-1]
        self.assertEqual((line["actor"], line["decision"]), ("person:page", {"by": "person", "via": "page"}))
        code, undone = self.post({"op": "undo", "journal": done["journal"]})
        self.assertEqual(code, 200, undone)
        self.assertEqual(undone["status"], "undone")
        self.assertEqual(self.stored()["auto"]["merged_branch_idle_days"], before)
        audit = [json.loads(x) for x in self.ks.AUDIT.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([a["subject"] for a in audit], ["plan", "apply", "undo"])
        self.assertEqual(audit[1]["keys"], ["cleanup.auto.merged_branch_idle_days"])

    def test_an_unknown_key_and_a_key_this_page_does_not_offer_are_refused(self):
        code, said = self.post({"op": "plan", "changes": {"guard.nothing": 1}})
        self.assertEqual(code, 400, said)
        self.assertIn("not a setting", said["error"])
        code, said = self.post({"op": "plan", "changes": {"integrations.vulnerabilities": False}})
        self.assertEqual(code, 400, said)
        self.assertIn("observatory_configure", said["error"])
        code, said = self.post({"op": "plan", "changes": {"cleanup.auto.merged_branch_idle_days": "soon"}})
        self.assertEqual(code, 400, said)
        code, said = self.post({"op": "erase"})
        self.assertEqual(code, 400, said)
        self.assertEqual(self.journal(), [])

    def test_a_plan_made_elsewhere_for_another_key_is_not_applied_here(self):
        import configuration_door
        plan = configuration_door.plan({"interface.locale": "ru"})
        code, said = self.post({"op": "apply", "digest": plan["digest"]})
        self.assertEqual(code, 400, said)
        self.assertEqual(self.journal(), [])

    def test_refused_without_the_token(self):
        code, said = self.post({"op": "plan", "changes": {"guard.temp_days.normal": 2}}, token=None)
        self.assertEqual(code, 401, said)
        code, said = self.post({"op": "plan", "changes": {"guard.temp_days.normal": 2}}, token="wrong")
        self.assertEqual(code, 401, said)
        self.assertEqual(self.journal(), [])


if __name__ == "__main__":
    unittest.main()
