#!/usr/bin/env python3
"""Anonymous usage and error events (OBS-324): on by default, off by a switch, never a private fact.

Every case runs in a temporary workspace with a fake transport: no test reaches the analytics host.
What each class holds:

- Switch: absent means on (an install that predates the release is on after it), only `off` is off,
  `OBSERVATORY_ANALYTICS=off` and `DO_NOT_TRACK=1` stop one process, the environment never turns on
  what a person turned off, and off deletes the install id and the queue.
- Record: only catalogued events and properties are queued; off, no App-Key or no workspace writes
  nothing; a full queue or an unwritable folder is counted, never raised.
- Sink: every event that leaves carries a random UUID `iid`, an allowed `environment` and `platform`,
  and nothing that names a path, the workspace or a person.
- Flush: batches of at most 25, expired events dropped, 400 and 404 never retried, a transport error
  or 5xx retried then kept, a test sandbox never sent.
- Notice: once on a terminal, never into a log, never when off; the dashboard's for seven days.
- Counters (OBS-341): updates are serialised by a lock, so concurrent writers (threads or processes)
  lose no count and no `last_send`; a replace the OS refuses for a moment is tried again, and one it keeps
  refusing is named in `status` as `degraded`, never raised.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import usage_analytics as UA  # noqa: E402

KEY = "A-SH-0000000000"
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


class Fake:
    """The transport: records every request, answers from a script of statuses."""

    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.requests: list[tuple[str, list, dict]] = []

    def __call__(self, url, body, headers, timeout):
        self.requests.append((url, json.loads(body), dict(headers)))
        status = self.statuses.pop(0) if self.statuses else 200
        if isinstance(status, Exception):
            raise status
        return status


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "workspace"
        self.base.mkdir()
        (self.base / "workspace.json").write_text("{}", encoding="utf-8")
        self.env = {UA.APP_KEY_ENV: KEY, UA.ENVIRONMENT_ENV: "production"}
        UA.WRITE_PROBLEM = None
        self.addCleanup(setattr, UA, "WRITE_PROBLEM", None)

    def rec(self, name="command_run", env=None, **props):
        props = props or {"surface": "cli", "command": "doctor", "outcome": "ok", "exit_code": 0, "duration_ms": 12}
        return UA.record(name, self.base, self.env if env is None else env, **props)

    def queued(self) -> list[dict]:
        try:
            text = (UA.folder(self.base) / "queue.jsonl").read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in text.splitlines()]

    def flush(self, fake, **kw):
        return UA.flush(self.base, transport=fake, environ=self.env, sleep=lambda s: None, **kw)


class Switch(Base):
    def test_absent_file_is_on_so_an_existing_install_is_on_after_the_update(self):
        self.assertIsNone(UA.read_switch(self.base))
        self.assertTrue(UA.enabled(self.base, {}))
        self.assertEqual(UA.status(self.base, self.env)["source"], "default")

    def test_only_off_is_off_and_the_choice_survives(self):
        UA.write_switch(self.base, False)
        self.assertFalse(UA.enabled(self.base, {}))
        self.assertEqual(UA.status(self.base, {})["source"], "file")
        UA.switch_path(self.base).write_text("maybe\n", encoding="utf-8")
        self.assertTrue(UA.enabled(self.base, {}))

    def test_environment_turns_one_process_off_and_never_on(self):
        for env in ({"OBSERVATORY_ANALYTICS": "off"}, {"OBSERVATORY_ANALYTICS": "0"},
                    {"OBSERVATORY_ANALYTICS": "FALSE"}, {"DO_NOT_TRACK": "1"}, {"DO_NOT_TRACK": "true"}):
            with self.subTest(env=env):
                self.assertFalse(UA.enabled(self.base, env))
                self.assertIsNone(UA.read_switch(self.base), "the file is not touched")
        self.assertTrue(UA.enabled(self.base, {"DO_NOT_TRACK": "0"}))
        self.assertTrue(UA.enabled(self.base, {"DO_NOT_TRACK": ""}))
        UA.write_switch(self.base, False)
        self.assertFalse(UA.enabled(self.base, {"OBSERVATORY_ANALYTICS": "on"}))

    def test_off_deletes_the_install_id_and_the_queue_and_on_makes_a_new_id(self):
        self.assertTrue(self.rec())
        first = UA.install_id(self.base)
        UA.write_switch(self.base, False)
        self.assertEqual(self.queued(), [])
        self.assertIsNone(UA.install_id(self.base, create=False))
        UA.write_switch(self.base, True)
        self.assertNotEqual(UA.install_id(self.base), first)

    def test_status_names_the_key_source_never_the_key(self):
        doc = UA.status(self.base, self.env)
        self.assertEqual(doc["app_key"], "env")
        self.assertNotIn(KEY, json.dumps(doc))
        self.assertTrue(doc["collecting"])
        absent = UA.status(self.base, {})
        self.assertEqual(absent["app_key"], "absent")
        self.assertFalse(absent["collecting"])
        self.assertIn("no analytics App-Key", absent["note"])

    def test_a_malformed_key_is_no_key(self):
        self.assertEqual(UA.app_key({UA.APP_KEY_ENV: "not-a-key"}), (None, "absent"))


class Record(Base):
    def test_a_catalogued_event_is_queued_with_its_properties_only(self):
        self.assertTrue(self.rec())
        [item] = self.queued()
        self.assertEqual(item["n"], "command_run")
        self.assertEqual(set(item["p"]), {"surface", "command", "outcome", "exit_code", "duration_ms"})
        self.assertEqual(item["e"], "production")

    def test_an_unknown_event_or_property_or_value_is_refused_and_counted(self):
        self.assertFalse(UA.record("project_opened", self.base, self.env, surface="cli"))
        self.assertFalse(self.rec(surface="cli", command="doctor", outcome="ok", exit_code=0, duration_ms=1,
                                  path="/srv/someone"))
        self.assertFalse(self.rec(surface="cli", command="/srv/someone/x", outcome="ok", exit_code=0, duration_ms=1))
        self.assertFalse(self.rec(surface="cli", command="doctor", outcome="maybe", exit_code=0, duration_ms=1))
        self.assertFalse(self.rec(surface="cli", command="doctor", outcome="ok", exit_code="0", duration_ms=1))
        self.assertFalse(self.rec(surface="cli", command="doctor", outcome="ok", exit_code=0))
        self.assertEqual(self.queued(), [])
        self.assertEqual(UA.counters(self.base)["refused"], 6)

    def test_off_no_key_or_no_workspace_writes_nothing(self):
        self.assertFalse(self.rec(env={UA.APP_KEY_ENV: KEY, "DO_NOT_TRACK": "1"}))
        self.assertFalse(self.rec(env={}))
        self.assertFalse(UA.folder(self.base).exists())
        (self.base / "workspace.json").unlink()
        self.assertFalse(self.rec())
        self.assertFalse(UA.folder(self.base).exists(), "a folder that is no workspace gets nothing")

    def test_a_full_queue_counts_the_drop(self):
        self.assertTrue(self.rec())
        queue = UA.folder(self.base) / "queue.jsonl"
        queue.write_bytes(b"x" * UA.QUEUE_LIMIT_BYTES)
        self.assertFalse(self.rec())
        self.assertEqual(UA.counters(self.base)["dropped_queue_full"], 1)

    def test_an_unwritable_folder_never_raises(self):
        (self.base / "store").mkdir()
        (self.base / "store" / "analytics").write_text("a file where the folder goes", encoding="utf-8")
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertFalse(self.rec())
        self.assertIn("FileExistsError", UA.WRITE_PROBLEM, "the lost count is named, by its class only")

    def test_error_kind_is_the_class_name_never_the_message(self):
        self.assertEqual(UA.error_kind(OSError("/srv/someone/secret")), "OSError")
        self.assertTrue(UA.record("error_raised", self.base, self.env, surface="cli", kind="OSError"))
        self.assertNotIn("someone", (UA.folder(self.base) / "queue.jsonl").read_text(encoding="utf-8"))


class Sink(Base):
    def test_every_event_that_leaves_carries_iid_environment_and_platform(self):
        for _ in range(3):
            self.rec()
        UA.record("tick_finished", self.base, self.env, surface="tick", outcome="ok", steps_failed=0, duration_ms=5)
        fake = Fake()
        self.assertEqual(self.flush(fake)["sent"], 4)
        [(url, events, headers)] = fake.requests
        self.assertEqual(url, "https://analytics.sshlg.me/api/v0/events")
        self.assertEqual(headers["App-Key"], KEY)
        iids = {ev["props"]["iid"] for ev in events}
        self.assertEqual(len(iids), 1)
        for ev in events:
            self.assertRegex(ev["props"]["iid"], UUID4)
            self.assertIn(ev["props"]["environment"], ("production", "sandbox"))
            self.assertIn(ev["props"]["platform"], ("macos", "server"))
            self.assertFalse(ev["systemProps"]["isDebug"], "production is the Release dataset")
            self.assertTrue(ev["systemProps"]["sdkVersion"])
            self.assertTrue(ev["systemProps"]["osName"])

    def test_sandbox_is_debug(self):
        self.env[UA.ENVIRONMENT_ENV] = "sandbox"
        self.rec()
        fake = Fake()
        self.flush(fake)
        ev = fake.requests[0][1][0]
        self.assertEqual(ev["props"]["environment"], "sandbox")
        self.assertTrue(ev["systemProps"]["isDebug"])

    def test_nothing_private_leaves(self):
        """The body sent is scanned for the workspace path, the home folder and the user name."""
        self.rec()
        UA.record("mcp_tool_called", self.base, self.env, surface="mcp", tool="observatory_status",
                  outcome="ok", duration_ms=3)
        UA.record("error_raised", self.base, self.env, surface="mcp", kind="ValueError")
        fake = Fake()
        self.flush(fake)
        body = json.dumps(fake.requests[0][1])
        for private in {str(self.base), str(Path.home()), os.environ.get("USER") or "\0", os.sep + "Users" + os.sep}:
            if len(private) > 2:
                self.assertNotIn(private, body)
        allowed = {"iid", "environment", "platform"}
        for ev in fake.requests[0][1]:
            self.assertLessEqual(set(ev["props"]), set(UA.EVENTS[ev["eventName"]]) | allowed)

    def test_the_sink_refuses_a_line_someone_edited_into_the_queue(self):
        self.rec()
        queue = UA.folder(self.base) / "queue.jsonl"
        item = self.queued()[0]
        item["p"]["path"] = "/srv/someone"
        queue.write_text(json.dumps(item) + "\n", encoding="utf-8")
        fake = Fake()
        out = self.flush(fake)
        self.assertEqual(out["unreadable"], 1)
        self.assertEqual(fake.requests, [])


class Flush(Base):
    def test_the_last_send_is_recorded_for_the_connections_declaration(self):
        self.rec()
        self.flush(Fake(200))
        self.assertTrue(UA.counters(self.base)["last_send"]["ok"])
        self.rec()
        self.flush(Fake(503, 503, 503))
        self.assertFalse(UA.counters(self.base)["last_send"]["ok"])
        self.flush(Fake(200))
        self.assertTrue(UA.counters(self.base)["last_send"]["ok"], "the kept event went at the next pass")
        before = UA.counters(self.base)["last_send"]
        self.assertEqual(self.flush(Fake())["result"], "empty")
        self.assertEqual(UA.counters(self.base)["last_send"], before, "an empty queue reached nothing: no claim")

    def test_batches_of_at_most_twenty_five(self):
        for _ in range(60):
            self.rec()
        fake = Fake()
        out = self.flush(fake)
        self.assertEqual([len(r[1]) for r in fake.requests], [25, 25, 10])
        self.assertEqual(out["sent"], 60)
        self.assertEqual(self.queued(), [])
        self.assertEqual(UA.counters(self.base)["sent"], 60)

    def test_an_event_older_than_the_host_accepts_is_dropped(self):
        self.rec()
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
        fake = Fake()
        out = self.flush(fake, now=later)
        self.assertEqual(out["expired"], 1)
        self.assertEqual(fake.requests, [])

    def test_400_and_404_are_never_retried(self):
        for status in (400, 404):
            with self.subTest(status=status):
                self.rec()
                fake = Fake(status)
                out = self.flush(fake)
                self.assertEqual(len(fake.requests), 1)
                self.assertEqual(out["rejected"], 1)
                self.assertEqual(self.queued(), [])

    def test_a_transport_error_or_5xx_is_retried_then_kept(self):
        self.rec()
        fake = Fake(OSError("down"), 503, 429)
        out = self.flush(fake)
        self.assertEqual(len(fake.requests), UA.ATTEMPTS)
        self.assertEqual(out["failed"], 1)
        [kept] = self.queued()
        self.assertEqual(kept["n"], "command_run")
        self.assertNotIn("iid", kept["p"], "the queue keeps the event, not the sink's properties")
        fake = Fake(503, 200)
        self.assertEqual(self.flush(fake)["sent"], 1)
        self.assertEqual(UA.counters(self.base)["send_failed"], 1)

    def test_off_drops_the_queue_at_the_next_flush(self):
        self.rec()
        self.env["DO_NOT_TRACK"] = "1"
        fake = Fake()
        self.assertEqual(self.flush(fake)["result"], "off")
        self.assertEqual(fake.requests, [])
        self.assertEqual(self.queued(), [])

    def test_a_test_sandbox_never_reaches_the_network(self):
        self.rec()
        self.env["OBSERVATORY_TEST_SANDBOX"] = "1"
        out = UA.flush(self.base, environ=self.env)
        self.assertEqual(out["result"], "test-sandbox")
        self.assertEqual(len(self.queued()), 1)

    def test_no_key_sends_nothing(self):
        self.assertEqual(UA.flush(self.base, transport=Fake(), environ={})["result"], "no-app-key")


class Counters(Base):
    """OBS-341: `count` and `note_send` read, change and write counters.json under one lock."""

    def test_concurrent_threads_lose_no_count_and_no_last_send(self):
        real = UA._read_json

        def slow(path):  # widen the read-to-write window so an unserialised update would be lost
            doc = real(path)
            time.sleep(0.002)
            return doc

        at = datetime.datetime(2026, 11, 5, 10, tzinfo=datetime.timezone.utc)

        def counting():
            for _ in range(15):
                UA.count(self.base, "sent")

        def sending():
            time.sleep(0.01)
            UA.note_send(self.base, ok=True, at=at)

        with mock.patch.object(UA, "_read_json", slow):
            workers = [threading.Thread(target=counting) for _ in range(6)] + [threading.Thread(target=sending)]
            for w in workers:
                w.start()
            for w in workers:
                w.join()
        doc = UA.counters(self.base)
        self.assertEqual(doc["sent"], 90)
        self.assertEqual(doc["last_send"], {"at": "2026-11-05T10:00:00Z", "ok": True})
        self.assertIsNone(UA.WRITE_PROBLEM)

    def test_concurrent_processes_lose_no_count_and_no_last_send(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import usage_analytics as UA; from pathlib import Path\n"
                "base = Path(sys.argv[2])\n"
                "for i in range(40):\n"
                "    UA.count(base, 'sent')\n"
                "    if i == 20 and sys.argv[3] == 'send': UA.note_send(base, ok=False)\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(self.base), role])
                 for role in ("count", "send", "count")]
        self.assertEqual([p.wait(timeout=60) for p in procs], [0, 0, 0])
        doc = UA.counters(self.base)
        self.assertEqual(doc["sent"], 120)
        self.assertIs(doc["last_send"]["ok"], False)

    def test_a_held_lock_is_waited_for_then_named_never_raised(self):
        import oslocks
        import osprivacy
        UA.count(self.base, "sent")
        fd = osprivacy.open(UA.folder(self.base) / ".counters.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        oslocks.flock(fd, oslocks.LOCK_EX)
        with mock.patch.object(UA, "COUNTERS_LOCK_SECONDS", 0.05), mock.patch("sys.stderr", io.StringIO()) as err:
            UA.count(self.base, "sent")
            UA.count(self.base, "sent")
        self.assertEqual(UA.counters(self.base)["sent"], 1, "nothing written while another writer holds the lock")
        self.assertIn("TimeoutError", UA.WRITE_PROBLEM)
        self.assertEqual(err.getvalue().count("usage-analytics:"), 1, "logged once, not per call")
        self.assertEqual(UA.status(self.base, self.env)["degraded"][0]["source"], "analytics-counters")
        oslocks.flock(fd, oslocks.LOCK_UN)
        UA.count(self.base, "sent")
        self.assertEqual(UA.counters(self.base)["sent"], 2)

    def test_a_replace_refused_for_a_moment_is_tried_again(self):
        real, calls = os.replace, []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) <= 2:
                raise PermissionError(13, "in use")
            return real(src, dst)

        with mock.patch.object(UA, "REPLACE_PAUSE_SECONDS", 0), mock.patch.object(UA.os, "replace", flaky):
            UA.note_send(self.base, ok=True)
        self.assertEqual(len(calls), 3)
        self.assertTrue(UA.counters(self.base)["last_send"]["ok"])
        self.assertIsNone(UA.WRITE_PROBLEM)

    def test_a_replace_always_refused_is_named_in_status_never_lost_silently(self):
        UA.count(self.base, "sent")

        def refused(src, dst):
            raise PermissionError(13, "in use")

        with mock.patch.object(UA, "REPLACE_PAUSE_SECONDS", 0), mock.patch.object(UA.os, "replace", refused), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            UA.note_send(self.base, ok=True)
        self.assertNotIn("last_send", UA.counters(self.base))
        self.assertIn("PermissionError", UA.WRITE_PROBLEM)
        self.assertNotIn(str(self.base), err.getvalue() + UA.WRITE_PROBLEM, "no path in the reason")
        self.assertEqual(UA.status(self.base, self.env)["degraded"],
                         [{"source": "analytics-counters", "reason": UA.WRITE_PROBLEM}])
        self.assertEqual([p.name for p in UA.folder(self.base).iterdir() if p.name.endswith(".tmp")], [],
                         "no temporary file is left behind")


class Clock:
    """A fake monotonic clock: the transport and the backoff advance it, nothing really waits."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class Bounded(Base):
    """M1 and L1 of the #421 review: a flush runs inside the maintenance pass's lock, so it has a total budget,
    stops at the first batch the host does not take, and stops at once when the switch goes off."""

    def fill(self, n=60):
        for _ in range(n):
            self.rec()

    def test_an_unreachable_host_holds_the_pass_for_the_budget_at_most(self):
        self.fill()
        clock, calls = Clock(), []

        def timing_out(url, body, headers, timeout):
            calls.append(timeout)
            clock.t += timeout
            raise OSError("timed out")
        out = UA.flush(self.base, transport=timing_out, environ=self.env, sleep=clock.sleep, clock=clock)
        self.assertLessEqual(clock.t - 1000.0, UA.FLUSH_BUDGET_SECONDS)
        self.assertLessEqual(len(calls), UA.ATTEMPTS, "the first batch that fails on transport ends the flush")
        self.assertEqual(out["sent"], 0)
        self.assertEqual(len(self.queued()), 60, "every event the flush did not send stays queued")

    def test_a_slow_host_is_cut_at_the_budget_and_the_rest_stays_queued(self):
        self.fill(100)
        clock, sent = Clock(), []

        def slow(url, body, headers, timeout):
            clock.t += 12
            sent.append(len(json.loads(body)))
            return 200
        out = UA.flush(self.base, transport=slow, environ=self.env, sleep=clock.sleep, clock=clock)
        self.assertLessEqual(clock.t - 1000.0, UA.FLUSH_BUDGET_SECONDS + 12)
        self.assertEqual(out["sent"] + len(self.queued()), 100)
        self.assertEqual((out["sent"], out["result"]), (75, "budget"))

    def test_each_request_gets_at_most_what_is_left_of_the_budget(self):
        self.fill(5)
        clock, timeouts = Clock(), []

        def fail(url, body, headers, timeout):
            timeouts.append(timeout)
            clock.t += timeout
            return 503
        UA.flush(self.base, transport=fail, environ=self.env, sleep=clock.sleep, clock=clock, budget=7.0)
        self.assertLessEqual(clock.t - 1000.0, 7.0)
        self.assertTrue(all(t <= UA.TIMEOUT_SECONDS for t in timeouts))

    def test_off_during_a_flush_stops_sending_and_keeps_nothing(self):
        self.fill()
        requests = []

        def turns_off(url, body, headers, timeout):
            requests.append(1)
            UA.write_switch(self.base, False)
            return 503
        UA.flush(self.base, transport=turns_off, environ=self.env, sleep=lambda s: None)
        self.assertEqual(len(requests), 1, "no retry and no next batch once the switch is off")
        self.assertFalse((UA.folder(self.base) / "queue.jsonl").exists(), "nothing is written back after off")

    def test_off_after_a_sent_batch_stops_before_the_next(self):
        self.fill()
        requests = []

        def ok_then_off(url, body, headers, timeout):
            requests.append(1)
            UA.write_switch(self.base, False)
            return 200
        UA.flush(self.base, transport=ok_then_off, environ=self.env, sleep=lambda s: None)
        self.assertEqual(len(requests), 1)
        self.assertFalse((UA.folder(self.base) / "queue.jsonl").exists())

    def test_the_real_transport_is_bounded_even_when_name_resolution_hangs(self):
        import time as _time
        started = _time.monotonic()
        with self.assertRaises(OSError):
            UA._bounded(lambda: _time.sleep(5), 0.2)
        self.assertLess(_time.monotonic() - started, 2)
        self.assertEqual(UA._bounded(lambda: 204, 1.0), 204)


class OsVersion(unittest.TestCase):
    def test_linux_sends_major_and_minor_only(self):
        self.assertEqual(UA.os_version("linux", "6.5.0-1025-azure"), "6.5")
        self.assertEqual(UA.os_version("linux", "5.15.167.4-microsoft-standard-WSL2"), "5.15")
        self.assertEqual(UA.os_version("linux", "weird"), "")
        self.assertEqual(UA.os_version("win32", "11"), "11")
        self.assertEqual(UA.os_version("darwin", "", "26.0.1"), "26.0.1")


class Notice(Base):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    def test_once_on_a_terminal(self):
        out = self.Tty()
        self.assertTrue(UA.cli_notice(self.base, out, self.env))
        self.assertIn("project-observatory full analytics off", out.getvalue())
        self.assertIn("never sends keys, paths", out.getvalue())
        self.assertFalse(UA.cli_notice(self.base, self.Tty(), self.env))
        [shown] = [q for q in self.queued() if q["n"] == "analytics_notice_shown"]
        self.assertEqual(shown["p"], {"surface": "cli"})

    def test_never_into_a_log_and_never_when_off(self):
        self.assertFalse(UA.cli_notice(self.base, io.StringIO(), self.env))
        self.assertEqual(UA.notice_state(self.base), {})
        self.assertFalse(UA.cli_notice(self.base, self.Tty(), {**self.env, "DO_NOT_TRACK": "1"}))

    def test_the_dashboard_shows_it_for_seven_days_from_a_persons_first_view(self):
        """L2 of the #421 review: the tick's build never marks it shown; the local server does when it serves Home."""
        start = datetime.datetime(2026, 11, 11, tzinfo=datetime.timezone.utc)
        for days in (0, 30):
            self.assertTrue(UA.dashboard_notice(self.base, start + datetime.timedelta(days=days), self.env),
                            "until someone opens Home, the built page keeps the notice")
        self.assertEqual(UA.notice_state(self.base), {}, "building the page marks nothing")
        self.assertEqual([q for q in self.queued() if q["n"] == "analytics_notice_shown"], [])
        self.assertTrue(UA.dashboard_viewed(self.base, start + datetime.timedelta(days=1), self.env))
        self.assertFalse(UA.dashboard_viewed(self.base, start + datetime.timedelta(days=2), self.env), "once")
        [shown] = [q for q in self.queued() if q["n"] == "analytics_notice_shown"]
        self.assertEqual(shown["p"], {"surface": "dashboard"})
        self.assertTrue(UA.dashboard_notice(self.base, start + datetime.timedelta(days=7), self.env))
        self.assertFalse(UA.dashboard_notice(self.base, start + datetime.timedelta(days=8), self.env))
        self.assertFalse(UA.dashboard_notice(self.base, start, {"DO_NOT_TRACK": "1"}))

    def test_a_view_while_off_marks_nothing(self):
        self.assertFalse(UA.dashboard_viewed(self.base, None, {**self.env, "DO_NOT_TRACK": "1"}))
        self.assertEqual(UA.notice_state(self.base), {})


class NoKey(Base):
    def test_a_build_without_a_key_announces_nothing(self):
        env = {UA.ENVIRONMENT_ENV: "production"}
        self.assertFalse(UA.cli_notice(self.base, Notice.Tty(), env))
        self.assertFalse(UA.dashboard_notice(self.base, None, env))
        self.assertFalse(UA.folder(self.base).exists())


class Hooks(unittest.TestCase):
    """The engine's own emits, end to end: a real workspace, the real CLI and MCP server, no network."""

    def setUp(self):
        import subprocess
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "home").mkdir()
        self.ws = self.root / "ws"
        self.env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.root / "home"),
                    "OBSERVATORY_HOME": str(self.ws), "OBSERVATORY_SYSTEM_SETUP": "0",
                    "OBSERVATORY_TEST_SANDBOX": str(self.root), "PYTHONDONTWRITEBYTECODE": "1",
                    UA.APP_KEY_ENV: KEY}
        for name in ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "SYSTEMROOT", "TEMP", "TMP", "PYTHONUTF8"):
            if os.environ.get(name):
                self.env[name] = os.environ[name]
        self.run_cli("init")
        try:
            (UA.folder(self.ws) / "queue.jsonl").unlink()
        except FileNotFoundError:
            pass

    def run_cli(self, *argv, env=None):
        import subprocess
        return subprocess.run([sys.executable, str(ROOT / "observatory.py"), *argv], cwd=str(ROOT),
                              env=env or self.env, capture_output=True, text=True, timeout=300)

    def queued(self):
        try:
            return [json.loads(x) for x in (UA.folder(self.ws) / "queue.jsonl").read_text(encoding="utf-8").splitlines()]
        except FileNotFoundError:
            return []

    def test_a_command_is_counted_by_its_name_and_never_its_arguments(self):
        self.assertEqual(self.run_cli("version").returncode, 0)
        self.assertEqual(self.run_cli("frobnicate", str(self.ws / "private-project")).returncode, 2)
        runs = [q["p"] for q in self.queued() if q["n"] == "command_run"]
        self.assertEqual([(r["command"], r["outcome"]) for r in runs], [("version", "ok"), ("other", "refused")])
        self.assertNotIn("private-project", (UA.folder(self.ws) / "queue.jsonl").read_text(encoding="utf-8"))

    def test_off_counts_nothing_and_doctor_says_so(self):
        self.assertEqual(self.run_cli("analytics", "off").returncode, 0)
        self.run_cli("version")
        self.assertEqual(self.queued(), [])
        doc = json.loads(self.run_cli("doctor").stdout)
        self.assertFalse(doc["analytics"]["on"])
        self.assertEqual(doc["analytics"]["turn_off"], "project-observatory full analytics off")
        self.assertEqual(self.run_cli("analytics", "on").returncode, 0)
        self.assertTrue(json.loads(self.run_cli("analytics", "status").stdout)["on"])

    def test_do_not_track_stops_one_process(self):
        self.run_cli("version", env={**self.env, "DO_NOT_TRACK": "1"})
        self.assertEqual(self.queued(), [])
        self.assertIsNone(UA.read_switch(self.ws))

    def test_an_mcp_tool_call_is_counted_by_the_tool_name(self):
        try:
            import anyio
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            self.skipTest("the MCP SDK is not installed in this interpreter")

        async def go():
            params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp/server.py")],
                                           cwd=str(ROOT), env=self.env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.discover()
                    await session.call_tool("observatory_overview", {})
                    await session.call_tool("no_such_tool_here", {})
        anyio.run(go)
        calls = [q["p"] for q in self.queued() if q["n"] == "mcp_tool_called"]
        self.assertIn(("observatory_overview", "ok"), [(c["tool"], c["outcome"]) for c in calls])
        self.assertIn(("other", "error"), [(c["tool"], c["outcome"]) for c in calls])


class Environment(Base):
    def test_forced_and_derived(self):
        self.assertEqual(UA.environment({UA.ENVIRONMENT_ENV: "sandbox"}), "sandbox")
        # This suite runs from a checkout, not an installed wheel.
        self.assertEqual(UA.environment({}), "sandbox" if "site-packages" not in str(UA.config.SOURCE) else "production")


class Command(Base):
    def test_off_on_status_through_main(self):
        import contextlib
        old = os.environ.get("OBSERVATORY_HOME")
        os.environ["OBSERVATORY_HOME"] = str(self.base)
        self.addCleanup(lambda: os.environ.__setitem__("OBSERVATORY_HOME", old) if old is not None
                        else os.environ.pop("OBSERVATORY_HOME", None))
        import configuration
        real = configuration.validate_workspace
        configuration.validate_workspace = lambda base=None, required=False: {}
        self.addCleanup(lambda: setattr(configuration, "validate_workspace", real))
        for action, on in (("off", False), ("status", False), ("on", True)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(UA.main([action]), 0)
            doc = json.loads(buf.getvalue())
            self.assertEqual(doc["on"], on and UA.env_off() is None)
            self.assertIn("never_sent", doc)


if __name__ == "__main__":
    unittest.main()
