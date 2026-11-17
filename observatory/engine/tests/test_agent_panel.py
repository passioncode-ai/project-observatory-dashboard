#!/usr/bin/env python3
"""OBS-560, CP-19: the built-in agent panel on the live dashboard.

The Assistant page carries the read-only assistant's chat and the use-case agents' notices in one place: the daily
doctor's last run (its fixes, each with an undo, and what it only reports) and the security watch (open findings of
the security group, by area, with a link to them). Every control reaches an existing MCP-backed function through the
key server: the chat through `observatory_assistant_status`, `observatory_assistant_ask`,
`observatory_assistant_conversation` and the assistant's jobs; an undo through `observatory_doctor`.

Opened as a file, the page is built with every control disabled and says why. Served by the key server, the ask
button is enabled only when the assistant can answer (switched on, a provider key, a model chain, a budget with room
today), with the cost note beside it; a refusal comes back as its code. No command line is shown on the page and the
question never reaches the key server's journal.
"""
from __future__ import annotations

import datetime
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

import agent_panel  # noqa: E402
import shell  # noqa: E402
from i18n import Translator  # noqa: E402

#: No command line a person would copy, on any part of the page.
COMMAND = re.compile(r"project-observatory full|doctor-daily|--undo|OBSERVATORY_HOME|configure budget|install_key")


def shown(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def relocalizes(test: unittest.TestCase, en: str, ru: str) -> None:
    import relocalize
    for built, reader in (("en", "ru"), ("ru", "en")):
        got = relocalize.relocalize({"en": en, "ru": ru}[built], reader)
        want = {"en": en, "ru": ru}[reader]
        test.assertTrue(got == want, f"{built}->{reader}: " + relocalize.first_difference(got, want))


DOCTOR = {"at": "2026-11-16T03:00:00Z",
          "checks": [{"id": "temp_orphans", "title": "Temporary files left by an interrupted write", "state": "fixed",
                      "count": 2},
                     {"id": "backup_stale", "title": "The workspace backup is older than 48 hours",
                      "state": "finding", "count": 1, "finding": "doctor.backup_stale"},
                     {"id": "mcp_doors", "title": "Engine servers running replaced code", "state": "finding",
                      "count": 1, "covered_by": "lifecycle.stale_servers"},
                     {"id": "vault_meta", "title": "Vault metadata files that cannot be read", "state": "ok",
                      "count": 0},
                     {"id": "plugin_behind", "title": "Installed plugins behind their marketplace",
                      "state": "unknown", "count": 0}],
          "fixes": [{"journal_id": "doc_0123456789abcdef", "check": "temp_orphans", "files": ["a.tmp", "b.tmp"]}],
          "undoable": [{"journal_id": "doc_0123456789abcdef", "check": "temp_orphans", "at": "2026-11-16T03:00:00Z",
                        "files": ["a.tmp", "b.tmp"]}],
          "summary": {"state": "written", "text": "Two leftover files were moved aside; the backup is old."},
          "degraded": [{"source": "harness", "reason": "the harness scan was not read; `full doctor-daily` runs it"}]}

FINDINGS = {"items": [
    {"id": "f1", "type": "secret.in_repository", "severity": "critical", "title": "A key in a repository"},
    {"id": "f2", "type": "mcp.tool_changed", "severity": "warning", "title": "A tool changed"},
    {"id": "f3", "type": "harness.skill_risk", "severity": "warning", "title": "A risky skill"},
    {"id": "f4", "type": "dep.vulnerable", "severity": "warning", "title": "A vulnerable package"},
    {"id": "f5", "type": "integration.key_invalid", "severity": "warning", "title": "A refused key"},
    {"id": "f6", "type": "site.down", "severity": "critical", "title": "A site is down"},
]}


def both(payload: dict) -> tuple[str, str]:
    return agent_panel.agent_panel_html(payload, Translator("en")), agent_panel.agent_panel_html(payload,
                                                                                               Translator("ru"))


class Page(unittest.TestCase):
    def test_opened_as_a_file_the_chat_is_built_disabled_and_says_why(self):
        en, ru = both({"doctor": DOCTOR, "findings": FINDINGS})
        text = shown(en)
        self.assertIn("Ask about the estate", text)
        self.assertIn("opened as a file", text)
        self.assertIn("paid from the engine's model budget", text, "the cost note is beside the button")
        send = re.search(r'<button[^>]*id="assistant-send"[^>]*>', en).group(0)
        self.assertIn(" disabled", send)
        self.assertIn("observatory_assistant_ask", send, "the tooltip names the agent's tool")
        self.assertRegex(en, r'<label[^>]*for="assistant-q"')
        self.assertRegex(en, r'<textarea[^>]*id="assistant-q"[^>]*maxlength="6000"')
        self.assertIn('id="assistant-transcript"', en)
        self.assertIn('aria-live="polite"', en)
        self.assertIn("Спросить", shown(ru))
        self.assertIsNone(COMMAND.search(en))
        self.assertIsNone(COMMAND.search(ru))
        relocalizes(self, en, ru)

    def test_the_doctor_lists_its_fixes_with_an_undo_and_what_it_only_reports(self):
        en, ru = both({"doctor": DOCTOR, "findings": FINDINGS})
        text = shown(en)
        self.assertIn("The daily doctor", text)
        self.assertIn("Two leftover files were moved aside", text, "the day's summary when one was written")
        self.assertIn("Temporary files left by an interrupted write", text)
        undo = re.search(r'<button[^>]*data-doctor-undo="doc_0123456789abcdef"[^>]*>', en).group(0)
        self.assertIn(" disabled", undo)
        self.assertIn("observatory_doctor", undo)
        self.assertRegex(text, r"Temporary files left by an interrupted write\s+2\s")
        # Only what needs reading is listed: a finding and an unread check; an ok check is a count.
        self.assertIn("The workspace backup is older than 48 hours", text)
        self.assertIn("Installed plugins behind their marketplace", text)
        self.assertNotIn("Vault metadata files that cannot be read", text)
        self.assertIn("already raised by another rule", text)
        self.assertIn("harness", text, "a source the doctor could not read is named")
        self.assertNotIn("full doctor-daily", en, "a degraded reason's command is not shown")
        self.assertIn('id="doctor-said"', en)
        self.assertIn("Ежедневная проверка", shown(ru))
        self.assertIsNone(COMMAND.search(en))
        relocalizes(self, en, ru)

    def test_a_doctor_that_never_ran_says_so_without_a_command(self):
        view = {"at": None, "checks": [], "fixes": [], "findings": [], "summary": None, "undoable": [],
                "degraded": [{"source": "doctor", "reason": "the daily doctor has not run; `full doctor-daily` runs it"}]}
        en, ru = both({"doctor": view, "findings": {"items": []}})
        self.assertIn("The daily doctor has not run yet", shown(en))
        self.assertNotIn("data-doctor-undo", en)
        self.assertIsNone(COMMAND.search(en))
        relocalizes(self, en, ru)
        en, _ = both({})
        self.assertIn("The daily doctor has not run yet", shown(en))
        self.assertIn("No security finding is open", shown(en))

    def test_the_security_watch_counts_open_findings_by_area_and_links_to_them(self):
        en, ru = both({"doctor": DOCTOR, "findings": FINDINGS})
        text = shown(en)
        self.assertIn("The security watch", text)
        self.assertIn("Open security findings: 5", text)
        self.assertRegex(text, r"Keys and secrets\s+1")
        self.assertRegex(text, r"Supply chain\s+2")
        self.assertRegex(text, r"Dependencies\s+1")
        self.assertRegex(text, r"Integrations\s+1")
        self.assertIn('href="findings.html?group=security"', en)
        self.assertIn("critical: 1", text)
        relocalizes(self, en, ru)

    def test_security_areas_cover_every_security_group_prefix(self):
        import finding_types
        for key, group in finding_types.GROUPS.items():
            if group == "security":
                probe = key + "x" if key.endswith((".", "_")) else key
                self.assertIn(agent_panel.area_of(probe), agent_panel.AREAS, key)


class Shell(unittest.TestCase):
    def test_the_assistant_page_is_in_the_work_group_after_findings(self):
        names = [p[0] for p in shell.PAGES]
        self.assertIn("assistant", names)
        work = dict((k, n) for k, _l, n in shell.NAV_GROUPS)["work"]
        self.assertEqual(work[work.index("findings") + 1], "assistant")
        self.assertIn("assistant", shell.QUESTIONS)

    def test_the_page_is_built_with_its_section_and_the_notices_ride_on_no_page_data(self):
        payload = {"doctor": DOCTOR, "findings": FINDINGS, "agents": {"x": 1}}
        self.assertIsNone(shell.slice_for("assistant", payload)["doctor"])
        self.assertIsNone(shell.slice_for("index", payload)["doctor"])
        import build_dashboard
        page = shell.page_html(shell.split_template(build_dashboard.template_for("en"))[0], "assistant", payload)
        self.assertIn('<div id="assistant"', page)
        self.assertIn('href="assistant.html" aria-current="page"', page)
        self.assertIn("Temporary files left by an interrupted write", page)
        self.assertIsNone(COMMAND.search(page.split("<script")[0]))

    def test_the_page_script_reaches_only_the_key_servers_existing_actions(self):
        import build_dashboard
        script = build_dashboard.template_for("en")
        block = script[script.index("(function agentPanel()"):]
        block = block[:block.index("})();") + 5]
        self.assertEqual(sorted(set(re.findall(r'call\("([a-z-]+)"', block))), ["assistant", "doctor"])
        for op in ("status", "ask", "job", "cancel", "conversation"):
            self.assertIn(f'op: "{op}"', block)
        self.assertIn('op: "undo"', block)
        self.assertNotIn("innerHTML = d", block, "answers are text, never markup")


# --- the assistant's readiness, as observatory_assistant_status answers it ----------------------------------------

class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.base = base
        self.home = base / "ws"
        # A clean environment, as the assistant's own suite: no inherited key, state or registry folder.
        keep = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP") if k in os.environ}
        self.env = patch.dict(os.environ, {**keep, "OBSERVATORY_HOME": str(self.home), "HOME": str(base / "user"),
                                           "USERPROFILE": str(base / "user")}, clear=True)
        self.env.start()
        import workspace
        workspace.initialize(self.home)
        import paths
        importlib.reload(paths)
        from agent import providers
        importlib.reload(providers)
        from agent import assistant
        self.a = importlib.reload(assistant)
        self.a.paths.REGISTRY.joinpath("projects.json").write_text(json.dumps({"projects": [
            {"id": "project:alpha-web", "name": "alpha-web", "lifecycle": "active"}]}), encoding="utf-8")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def configure_model(self, ceilings=(1.0, 10.0, 0.5)):
        models = self.a.paths.config_file("models.json")
        doc = json.loads(models.read_text(encoding="utf-8"))
        doc["chain"] = [{"id": "example/model-a", "why": "configured"}]
        doc["wallet"].update(daily_ceiling=ceilings[0], monthly_ceiling=ceilings[1], velocity_ceiling=ceilings[2])
        models.write_text(json.dumps(doc), encoding="utf-8")

    def spent_today(self, amount: float) -> None:
        day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        self.a.providers.WALLET.parent.mkdir(parents=True, exist_ok=True)
        self.a.providers.WALLET.write_text(json.dumps({"denomination": "credits", "events": [],
                                                       "days": {day: amount}, "months": {day[:7]: amount}}),
                                           encoding="utf-8")

    def enabled(self):
        from contextlib import ExitStack
        ctx = ExitStack()
        ctx.enter_context(patch.object(self.a.configuration, "enabled", return_value=True))
        ctx.enter_context(patch.object(self.a.providers, "have_key", return_value=True))
        ctx.enter_context(patch.object(self.a.providers, "key_report",
                                       return_value={"key_status": "present", "key_source": "a test"}))
        ctx.enter_context(patch.object(self.a.jobs, "_spawn", return_value=type("Proc", (), {"pid": os.getpid()})()))
        return ctx


class Readiness(Workspace):
    def test_status_says_why_the_assistant_cannot_answer_and_what_today_cost(self):
        doc = self.a.status(include_projects=False)
        self.assertFalse(doc["ready"])
        self.assertIn(doc["blocked_by"], ("agent-disabled", "provider-unconfigured", "model-unconfigured"))
        self.configure_model()
        with self.enabled():
            doc = self.a.status(include_projects=False)
            self.assertEqual((doc["ready"], doc["blocked_by"]), (True, None))
            self.assertEqual(doc["spend"], {"denomination": "credits", "today": 0.0, "month": 0.0,
                                            "daily_ceiling": 1.0, "monthly_ceiling": 10.0})
            self.spent_today(1.25)
            doc = self.a.status(include_projects=False)
            self.assertEqual((doc["ready"], doc["blocked_by"]), (False, "budget-reached"))
            self.assertEqual(doc["spend"]["today"], 1.25)
        self.configure_model(ceilings=(0.0, 10.0, 0.5))
        with self.enabled():
            self.assertEqual(self.a.status(include_projects=False)["blocked_by"], "budget-unset")
        with patch.object(self.a.configuration, "enabled", return_value=False):
            self.assertEqual(self.a.status(include_projects=False)["blocked_by"], "agent-disabled")

    def test_an_unreadable_journal_is_named_not_read_as_nothing_spent(self):
        self.configure_model()
        self.a.providers.WALLET.parent.mkdir(parents=True, exist_ok=True)
        self.a.providers.WALLET.write_text("{", encoding="utf-8")
        with self.enabled():
            doc = self.a.status(include_projects=False)
        self.assertIsNone(doc["spend"]["today"])
        self.assertIn("wallet", json.dumps(doc["degraded"]))


# --- the key server's routes ------------------------------------------------------------------------------------

TOKEN = "-".join(("a", "token", "only", "this", "suite", "knows"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Route(Workspace):
    """POST /api/assistant and /api/doctor on the key server: the assistant's and the doctor's own functions."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(ROOT / "tools"))
        import keyserver
        self.ks = keyserver
        self.audit = keyserver.AUDIT
        keyserver.AUDIT = self.base / "keyserver.jsonl"
        self.port = _free_port()
        self.srv = keyserver.Server(("127.0.0.1", self.port), keyserver.Handler, TOKEN)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        time.sleep(0.1)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.ks.AUDIT = self.audit
        super().tearDown()

    def post(self, action: str, body: dict, *, token: str | None = TOKEN) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json", "X-Observatory-Caller": "page:assistant",
                   "Origin": f"http://127.0.0.1:{self.port}"}
        if token:
            headers["X-Observatory-Token"] = token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/{action}", data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read().decode())

    def journal(self) -> str:
        return self.ks.AUDIT.read_text(encoding="utf-8") if self.ks.AUDIT.exists() else ""

    def test_status_answers_readiness_spend_and_conversations_without_a_command_or_a_path(self):
        code, doc = self.post("assistant", {"op": "status"})
        self.assertEqual(code, 200, doc)
        self.assertFalse(doc["ready"])
        self.assertIn(doc["blocked_by"], ("agent-disabled", "provider-unconfigured", "model-unconfigured"))
        for gone in ("workspace", "next", "key_source", "provider_status", "projects"):
            self.assertNotIn(gone, doc)
        self.assertIsNone(COMMAND.search(json.dumps(doc)))
        self.assertNotIn(str(self.home), json.dumps(doc))
        self.assertEqual(doc["conversations"], [])
        self.assertIn("spend", doc)
        self.assertEqual(doc["degraded"], [])

    def test_a_refused_ask_is_typed_and_starts_nothing(self):
        code, doc = self.post("assistant", {"op": "ask", "question": "What changed?", "request_id": "page-req-0001"})
        self.assertEqual((code, doc), (400, {"error": {"code": "agent-disabled", "message": "agent-disabled"}}))
        jobs_dir = self.a.jobs.jobs_dir()
        self.assertEqual(sorted(jobs_dir.glob("*.json")) if jobs_dir.exists() else [], [])

    def test_ask_then_poll_then_read_the_answer_and_the_question_stays_out_of_the_journal(self):
        self.configure_model()
        question = "Which project of alpha-web has unpushed work?"
        reply = {"parsed": {"answer": "None of them.", "evidence_ids": ["E1"], "next_steps": []},
                 "model": "fixture", "cost": 0}
        with self.enabled(), patch.object(self.a.providers, "complete", return_value=reply) as complete:
            code, asked = self.post("assistant", {"op": "ask", "question": question, "request_id": "page-req-0002"})
            self.assertEqual(code, 200, asked)
            job_id, cid = asked["job"]["id"], asked["conversation_id"]
            code, again = self.post("assistant", {"op": "ask", "question": question, "request_id": "page-req-0002"})
            self.assertEqual((code, again["job"]["id"]), (200, job_id), "a repeated request is the same job")
            code, polled = self.post("assistant", {"op": "job", "id": job_id})
            self.assertEqual(code, 200, polled)
            self.assertNotIn(polled["job"]["status"], ("completed",))
            self.a.run_question(self.a.jobs.get(job_id))             # the runner, in this process
            complete.assert_called_once()
            code, polled = self.post("assistant", {"op": "job", "id": job_id})
            self.assertEqual(polled["job"]["status"], "completed")
            self.assertNotIn("result", polled["job"], "the answer is read from the conversation")
            code, conv = self.post("assistant", {"op": "conversation", "id": cid})
            self.assertEqual(code, 200, conv)
            self.assertEqual(conv["turns"][0]["question"], question)
            self.assertEqual(conv["turns"][0]["answer"]["answer"], "None of them.")
            code, status = self.post("assistant", {"op": "status"})
            self.assertEqual([c["id"] for c in status["conversations"]], [cid])
        lines = [json.loads(x) for x in self.journal().splitlines()]
        self.assertEqual([(x["action"], x["subject"]) for x in lines], [("assistant", "ask"), ("assistant", "ask")])
        self.assertNotIn("unpushed", self.journal(), "the question never reaches the key server's journal")

    def test_job_and_cancel_answer_only_for_the_assistants_jobs(self):
        doc, _ = self.a.jobs.start("fixture", {}, spawn=lambda argv, env: type("Proc", (), {"pid": 12345})())
        for op in ("job", "cancel"):
            code, said = self.post("assistant", {"op": op, "id": doc["id"]})
            self.assertEqual((code, said), (404, {"error": {"code": "unknown-job", "message": "unknown-job"}}))
        self.assertNotEqual(self.a.jobs.get(doc["id"])["status"], "cancelled")
        self.configure_model()
        with self.enabled():
            code, asked = self.post("assistant", {"op": "ask", "question": "Q?", "request_id": "page-req-0003"})
            with patch.object(self.a.jobs.osproc, "signal_group") as stop:   # the runner's pid is this process
                code, said = self.post("assistant", {"op": "cancel", "id": asked["job"]["id"]})
        self.assertEqual((code, said["job"]["status"]), (200, "cancelled"))
        stop.assert_called_once()

    def test_malformed_input_and_a_key_in_the_question_are_refused(self):
        for body, want in (({"op": "erase"}, 400), ({"op": "ask", "question": "q", "request_id": "x"}, 400),
                           ({"op": "conversation", "id": "../../etc"}, 400), ({"op": "job"}, 400),
                           ({"op": "ask", "question": "q", "request_id": "page-req-0004", "shell": "rm"}, 400)):
            code, said = self.post("assistant", body)
            self.assertEqual(code, want, (body, said))
        self.configure_model()
        planted = "sk-or-v1-" + "Q" * 48
        with self.enabled():
            code, said = self.post("assistant", {"op": "ask", "question": f"is {planted} valid?",
                                                 "request_id": "page-req-0005"})
        self.assertEqual((code, said), (400, {"error": {"code": "credential-shaped-input", "message": "credential-shaped-input"}}))
        exposed = [str(p) for p in self.base.rglob("*") if p.is_file() and planted.encode() in p.read_bytes()]
        self.assertEqual(exposed, [], "the value reached a file")

    def test_the_doctors_undo_reverses_a_fix_and_refusals_are_typed(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import daily_doctor
        store = self.home / "store"
        (store / "left.tmp").write_text("x", encoding="utf-8")
        fix = daily_doctor._quarantine(self.home, "temp_orphans", ["left.tmp"],
                                       datetime.datetime.now(datetime.timezone.utc))
        self.assertFalse((store / "left.tmp").exists())
        code, done = self.post("doctor", {"op": "undo", "journalId": fix["journal_id"]})
        self.assertEqual(code, 200, done)
        self.assertEqual(done["undone"], [{"id": fix["journal_id"], "files": ["left.tmp"]}])
        self.assertTrue((store / "left.tmp").exists())
        code, said = self.post("doctor", {"op": "undo", "journalId": fix["journal_id"]})
        self.assertEqual((code, said), (400, {"error": {"code": "already-undone", "message": "already-undone"}}))
        code, said = self.post("doctor", {"op": "undo", "journalId": "doc_ffffffffffffffff"})
        self.assertEqual((code, said), (404, {"error": {"code": "unknown-journal-id", "message": "unknown-journal-id"}}))
        for body in ({"op": "undo", "journalId": "../x"}, {"op": "undo"}, {"op": "run"}):
            code, said = self.post("doctor", body)
            self.assertEqual(code, 400, (body, said))
        subjects = [json.loads(x)["subject"] for x in self.journal().splitlines()]
        self.assertEqual(subjects.count("undo"), 3)

    def test_refused_without_the_token(self):
        for action, body in (("assistant", {"op": "status"}), ("doctor", {"op": "undo", "journalId": "doc_" + "0" * 16})):
            self.assertEqual(self.post(action, body, token=None)[0], 401)
            self.assertEqual(self.post(action, body, token="wrong")[0], 401)

    def test_the_key_server_serves_the_page(self):
        page, ctype = self.ks.Handler._page_for(None, "/dashboard/assistant.html")
        self.assertEqual((page.name, ctype), ("assistant.html", "text/html"))


if __name__ == "__main__":
    unittest.main()
