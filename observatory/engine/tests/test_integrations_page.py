#!/usr/bin/env python3
"""OBS-557, CP-12 to CP-14: the Integrations page and the issue filer's one-time notice.

The page answers whether the held provider keys are still accepted, which collector answers changed shape, which
providers a project uses that nothing watches, and which issues were filed. A key is shown by its slot name and a
package or variable by its name. The notice says issues are filed and how to turn that off; its Dismiss button is
disabled until the local control server serves the page, and the server stores the dismissal.
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
sys.path.insert(0, str(ROOT / "tools"))

import home_page  # noqa: E402
import integrations_page  # noqa: E402
import shell  # noqa: E402
from i18n import Translator  # noqa: E402

COMMAND = re.compile(r"project-observatory full|--apply|OBSERVATORY_HOME")
TOKEN = "t" * 32
VALUE = "fakevalue-cccc-3333-not-a-real-key"


def shown(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def payload(**over) -> dict:
    doc = {
        "rows": [{"id": "project:alpha-web", "name": "Alpha Web"}],
        "integrations": {
            "checkedOn": "2026-11-16", "trackedProviders": ["cloudflare"],
            "keys": [{"provider": "cloudflare", "slot": "cloudflare/admin/old", "state": "invalid", "status": 401,
                      "checked_on": "2026-11-16", "since": "2026-11-14"},
                     {"provider": "cloudflare", "slot": "cloudflare/admin/ok", "state": "valid", "status": 200,
                      "checked_on": "2026-11-16"},
                     {"provider": "openrouter", "slot": "openrouter/admin/a", "state": "unreachable", "status": None,
                      "checked_on": "2026-11-16"}],
            "drift": [{"collector": "digitalocean", "field": "droplets", "last_seen": "2026-11-10", "rows": 2}],
            "untracked": [{"project": "project:alpha-web", "provider": "stripe", "category": "payments",
                           "evidence": [{"kind": "env", "name": "STRIPE_API_KEY"}], "evidence_total": 3}],
            "issues": {"issues": [{"repo": "example-org/alpha-web", "number": 12, "state": "open",
                                   "type": "dep.vulnerable", "filed_on": "2026-11-15"},
                                  {"repo": "example-org/beta-api", "number": None, "state": "not-filed",
                                   "type": "integration.key_invalid", "reason": "no write access"}],
                       "lastRun": {"date": "2026-11-16", "created": 1, "closed": 0, "deferred": 0},
                       "banner": {"show": True, "enabled": True, "dismissed_on": None}, "degraded": []},
            "degraded": [{"source": "integration:heroku", "reason": "keys not listed: OSError"}]}}
    doc.update(over)
    return doc


def both(p: dict) -> tuple[str, str]:
    return integrations_page.integrations_html(p, Translator("en")), integrations_page.integrations_html(p, Translator("ru"))


class Page(unittest.TestCase):
    def test_keys_by_provider_drift_untracked_and_issues(self):
        en, ru = both(payload())
        text = shown(en)
        for want in ("cloudflare", "cloudflare/admin/old", "refused", "401", "accepted", "openrouter/admin/a",
                     "not reachable", "digitalocean", "droplets", "2026-11-10", "Alpha Web", "stripe",
                     "STRIPE_API_KEY", "and 2 more", "example-org/alpha-web#12", "dep.vulnerable", "no write access",
                     "Not fully read", "integration:heroku"):
            self.assertIn(want, text)
        self.assertLess(text.index("cloudflare/admin/old"), text.index("cloudflare/admin/ok"), "refused keys first")
        self.assertIn("Состояние", shown(ru))
        self.assertIsNone(COMMAND.search(en))
        self.assertIsNone(COMMAND.search(ru))
        self.assertNotIn(VALUE, en)

    def test_a_page_before_any_check_says_so_and_never_shows_a_clean_bill(self):
        en, _ru = both({"rows": [], "integrations": {"keys": [], "drift": [], "untracked": [], "issues": {},
                                                     "degraded": [{"source": "integrations",
                                                                   "reason": "the integration health step has not run"}]}})
        text = shown(en)
        self.assertIn("No held key was checked yet", text)
        self.assertIn("Not fully read", text)
        self.assertIn("has not run", text)

    def test_relocalizes_between_languages(self):
        import relocalize
        en, ru = both(payload())
        for built, reader in (("en", "ru"), ("ru", "en")):
            got = relocalize.relocalize({"en": en, "ru": ru}[built], reader)
            want = {"en": en, "ru": ru}[reader]
            self.assertTrue(got == want, f"{built}->{reader}: " + relocalize.first_difference(got, want))


class Banner(unittest.TestCase):
    def test_shown_with_a_disabled_dismiss_and_the_way_to_turn_it_off(self):
        en, ru = both(payload())
        self.assertIn('id="issues-notice"', en)
        button = re.search(r'<button[^>]*data-issues-dismiss[^>]*>', en).group(0)
        self.assertIn(" disabled", button)
        self.assertIn("observatory_issues", button)
        self.assertIn("github_issues", shown(en))
        self.assertIn("issues-notice", ru)
        self.assertIn("issues-notice", home_page.home_html(payload(), Translator("en")), "Home carries it too")

    def test_not_shown_once_dismissed_or_while_off(self):
        p = payload()
        p["integrations"]["issues"]["banner"] = {"show": False, "enabled": True, "dismissed_on": "2026-11-16"}
        self.assertNotIn("issues-notice", integrations_page.integrations_html(p, Translator("en")))
        self.assertNotIn("issues-notice", home_page.home_html(p, Translator("en")))


class Navigation(unittest.TestCase):
    def test_the_page_is_in_monitoring_and_has_a_question_and_a_card(self):
        self.assertIn("integrations", [n for g, _l, names in shell.NAV_GROUPS if g == "monitoring" for n in names])
        self.assertIn("integrations", shell.QUESTIONS)
        cards = shell.cards_html(payload(), {"projects": 1, "findings": 0, "domains": 0, "heroku": 0, "creds": 0,
                                             "env": 0, "mcp": 0}, Translator("en"))
        self.assertIn('href="integrations.html"', cards)
        self.assertIn("1 key refused", shown(cards))

    def test_the_page_html_carries_the_body_and_its_data_slice_drops_the_payload_elsewhere(self):
        self.assertIsNone(shell.slice_for("index", payload())["integrations"])
        self.assertIsNotNone(shell.slice_for("integrations", payload())["integrations"])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Route(unittest.TestCase):
    """POST /api/issues-banner on the key server: the same store function as `observatory_issues`."""

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
        import issue_filer
        importlib.reload(issue_filer)
        self.filer = issue_filer
        # The runner's sandbox may fix the state folder for every test: start each from no map.
        issue_filer.default_path().unlink(missing_ok=True)
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
        headers = {"Content-Type": "application/json", "X-Observatory-Caller": "page:integrations",
                   "Origin": f"http://127.0.0.1:{self.port}"}
        if token:
            headers["X-Observatory-Token"] = token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/issues-banner", data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read().decode())

    def test_dismiss_stores_the_day_and_the_tool_sees_it(self):
        self.assertTrue(self.filer.agent_view()["banner"]["show"])
        code, done = self.post({"op": "dismiss"})
        self.assertEqual(code, 200, done)
        self.assertTrue(done["dismissed_on"])
        self.assertFalse(self.filer.agent_view()["banner"]["show"])
        audit = [json.loads(x) for x in self.ks.AUDIT.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(audit[-1]["action"], "issues-banner")

    def test_refuses_a_caller_without_the_token_and_an_unknown_op(self):
        self.assertEqual(self.post({"op": "dismiss"}, token=None)[0], 401)
        code, _ = self.post({"op": "reset"})
        self.assertIn(code, (400, 422))
        self.assertTrue(self.filer.agent_view()["banner"]["show"])


if __name__ == "__main__":
    unittest.main()
