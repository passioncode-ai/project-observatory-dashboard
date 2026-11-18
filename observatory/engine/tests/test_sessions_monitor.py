#!/usr/bin/env python3
"""The coding sessions watched with the host's load, with history (OBS-630).

Every source is a fixture: the account manager's CLI answers through an injected runner keyed by its command, its
sessions feed is a file in a temporary folder, the host's load, memory and disk are injected callables. Nothing runs a
real command or reads the network. Account labels, folders and groups are neutral names, and the tests assert that no
label and no full folder path reaches the summary, the history or a finding.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for sub in ("dashboard", "tools"):
    sys.path.append(str(ROOT / sub))

import tmp as tmpdir                                                               # noqa: E402
import sessions_monitor as SM                                                      # noqa: E402
import coding_session_findings as CSF                                              # noqa: E402
import sessions_view                                                               # noqa: E402
from i18n import Translator                                                        # noqa: E402

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
STAMP = int(NOW.timestamp())
BIN = "/usr/local/bin/switchboard"
LABEL_A, LABEL_B = "account-label-a", "account-label-b"
ALPHA = "/srv/example/projects/alpha-web"
BETA = "/srv/example/projects/beta-api"
GAMMA = "/srv/example/projects/gamma-app"


def machine_row(pid, folder, *, agent="claude-code", managed=False, launch=None, quiet_minutes=0):
    return {"agent": agent, "folder": folder, "last_write_at": STAMP - 60 * quiet_minutes, "launch_id": launch,
            "managed": managed, "pid": pid, "quiet": quiet_minutes >= 30, "quiet_minutes": quiet_minutes,
            "started_at": STAMP - 3600}


MACHINE = [
    machine_row(101, ALPHA, managed=True, launch="launch-1"),          # launched on team-a
    machine_row(102, ALPHA + "/docs"),                                  # in team-a's folder route
    machine_row(103, BETA),                                             # in no group folder
    machine_row(104, ALPHA, agent="codex"),                             # codex: a Claude Code route does not hold
    machine_row(105, GAMMA, quiet_minutes=41),                          # busy for 41 minutes with no turn
    machine_row(106, GAMMA, launch="launch-2", managed=True),           # launched on team-b in team-c's folder
]


def rows_row(pid, why="working", *, pool=None, account=None, launch=None, quiet_minutes=0, group="running"):
    return {"account": account, "acts": [], "agent": "claude-code", "folder": "/ignored", "group": group,
            "id": f"pid:{pid}", "launch_id": launch, "origin": None, "pid": pid, "pool": pool, "probe_at": None,
            "quiet_minutes": quiet_minutes, "session_id": None, "since": STAMP - 600, "task": None, "wait": None,
            "why": why}


ROWS = [rows_row(101, pool="team-a", account=LABEL_A, launch="launch-1"), rows_row(102), rows_row(103, "quiet"),
        rows_row(104), rows_row(105, quiet_minutes=41), rows_row(106, pool="team-b", account=LABEL_B, launch="launch-2"),
        rows_row(0, "exited", group="ended", pool="team-a")]
BINDINGS = [{"base_url": "http://127.0.0.1:40000/claude/team-a", "folder": ALPHA, "pool": "team-a"},
            {"base_url": "http://127.0.0.1:40000/claude/team-c", "folder": GAMMA, "pool": "team-c"}]


def account(label, pool, used, *, at=STAMP - 120, **extra):
    windows = [{"name": "five_hour", "resets_at": STAMP + 3600, "used_percent": used[0]},
               {"name": "seven_day", "resets_at": STAMP + 86400, "used_percent": used[1]}]
    return {"id": f"id-{label}", "label": label, "pool": pool, "provider": "claude", "kind": "oauth",
            "enabled": True, "elsewhere": False, "location": None, "assigned_to": None,
            "external_identity": {"email": f"{label}@example.com"},
            "usage": {"observed_at": at, "used_percent": max(used), "windows": windows}, **extra}


ACCOUNTS = [account(LABEL_A, "team-a", (10.0, 40.0)), account(LABEL_B, "team-b", (100.0, 20.0)),
            account("account-label-c", "team-b", (5.0, 100.0)),
            account("account-label-d", "team-c", (1.0, 1.0), elsewhere=True, location="other-host")]
CAPACITY = {"cap": 40, "used": 18, "free": 22, "total": 28, "board_share": 10, "board_used": 2, "board_free": 8,
            "observed_at": STAMP - 5}


def ok(data) -> str:
    return json.dumps({"ok": True, "data": data})


class Runner:
    """The manager's CLI: answers each command from a table; a value that is an exception is raised."""

    def __init__(self, **answers) -> None:
        self.answers = {"sessions --machine --json": ok({"observed_at": STAMP - 5, "sessions": MACHINE}),
                        "--json sessions --rows": ok({"observed_at": STAMP - 4, "rows": ROWS, "counts": {}}),
                        "--json project bindings": ok({"bindings": BINDINGS}),
                        "--json capacity": ok(CAPACITY),
                        "--json accounts list": ok({"accounts": ACCOUNTS, "routes": {}})}
        self.answers.update({k.replace("_", " "): v for k, v in answers.items()})
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        answer = self.answers.get(" ".join(argv[1:]))
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            return 2, ""
        return (answer if isinstance(answer, tuple) else (0, answer))


def host(view, now, degraded, **_):
    """The host as measured here, injected: load 6.0 on 4 cores, normal pressure, 2 GB swap, 40 GB free."""
    return SM.host_sample(view, now, degraded, loadavg=lambda: (6.0, 5.0, 4.0), cpu_count=lambda: 4,
                          memory=lambda scratch, at: {"pressure": {"level": "normal"}, "swap_used_mb": 2048},
                          disk_usage=lambda root: namedtuple("U", "total used free")(100e9, 60e9, 40e9))


class Home:
    def __init__(self) -> None:
        self.home = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-sessions-monitor-"))
        (self.home / "store" / "raw").mkdir(parents=True)  # paths-check: allow: a temporary workspace home
        self.feed = self.home / "feed" / SM.FEED

    def summary(self, runner=None, manager=BIN, **kw) -> dict:
        kw.setdefault("feed", self.feed)
        kw.setdefault("host", host)
        return sessions_view.summary(self.home, now=NOW, runner=runner or Runner(), manager=manager,
                                     attribute=lambda f: None, monitor=True, **kw)


def by_pid(doc: dict) -> dict:
    return {s["pid"]: s for s in doc["sessions"]}


class Route(unittest.TestCase):
    def test_a_session_in_a_group_folder_reads_managed_with_its_group(self) -> None:
        doc = Home().summary()
        s = by_pid(doc)
        self.assertEqual((s[101]["managed"], s[101]["route"], s[101]["account_group"]), (True, "launch", "team-a"))
        self.assertEqual((s[102]["managed"], s[102]["route"], s[102]["account_group"], s[102]["folder_group"]),
                         (True, "folder-route", "team-a", "team-a"))
        self.assertEqual((s[103]["managed"], s[103]["route"], s[103]["account_group"]), (False, None, None))
        self.assertEqual((s[104]["managed"], s[104]["route"]), (False, None), "a Claude Code route holds no codex")
        self.assertEqual(doc["managed"], 4)

    def test_the_group_that_serves_a_launch_is_its_accounts(self) -> None:
        s = by_pid(Home().summary())[106]
        self.assertEqual((s["account_group"], s["folder_group"]), ("team-b", "team-c"))

    def test_the_rows_say_whether_a_session_is_busy(self) -> None:
        s = by_pid(Home().summary())
        self.assertEqual((s[105]["manager_state"], s[105]["busy"], s[105]["quiet_minutes"]), ("working", True, 41))
        self.assertEqual((s[103]["manager_state"], s[103]["busy"]), ("quiet", False))

    def test_no_label_and_no_full_path_reaches_the_summary(self) -> None:
        said = json.dumps(Home().summary())
        for private in (LABEL_A, LABEL_B, "example.com", "/srv/example", "/ignored", "127.0.0.1"):
            self.assertNotIn(private, said)

    def test_each_command_is_bounded_and_read_only(self) -> None:
        runner = Runner()
        Home().summary(runner)
        asked = [" ".join(argv[1:]) for argv, _ in runner.calls]
        self.assertEqual(asked, ["sessions --machine --json", "--json sessions --rows", "--json project bindings",
                                 "--json capacity", "--json accounts list"])
        self.assertTrue(all(t <= SM.CLI_TIMEOUT for argv, t in runner.calls[1:]))

    def test_without_the_monitor_the_summary_is_what_it_was(self) -> None:
        runner = Runner()
        home = Home()
        doc = sessions_view.summary(home.home, now=NOW, runner=runner, manager=BIN, attribute=lambda f: None)
        self.assertEqual(len(runner.calls), 1)
        self.assertNotIn("host", doc)
        self.assertNotIn("route", doc["sessions"][0])


class Degraded(unittest.TestCase):
    def test_no_manager_and_no_feed_runs_nothing_and_still_samples_the_host(self) -> None:
        runner = Runner()
        doc = Home().summary(runner, manager=None)
        self.assertEqual(runner.calls, [])
        self.assertIsNone(doc["managed"])
        self.assertIsNone(doc["monitor"]["source"])
        self.assertEqual(doc["host"]["load_per_core"], 1.5)
        self.assertIsNone(doc["host"]["sessions_cap"])
        self.assertIsNone(doc["host"]["groups_with_room"], "groups not read are unknown, never none with room")
        self.assertIn("not-installed", {d["code"] for d in doc["degraded"]})
        self.assertEqual(CSF.monitor_findings(doc), [])

    def test_a_command_that_fails_is_named_and_the_rest_is_read(self) -> None:
        import subprocess
        doc = Home().summary(Runner(**{"--json_capacity": subprocess.TimeoutExpired("x", 5),
                                       "--json_accounts_list": (1, "")}))
        codes = {(d["source"], d["code"]) for d in doc["degraded"]}
        self.assertIn(("manager-capacity", "timeout"), codes)
        self.assertIn(("manager-accounts", "failed"), codes)
        self.assertIsNone(doc["capacity"])
        self.assertIsNone(doc["groups"])
        self.assertEqual(by_pid(doc)[102]["route"], "folder-route", "the routes were still read")
        self.assertNotIn("pool.no_room", {f["type"] for f in CSF.monitor_findings(doc)})

    def test_malformed_answers_are_named(self) -> None:
        doc = Home().summary(Runner(**{"--json_sessions_--rows": ok({"rows": "nope"}), "--json_project_bindings": "{"}))
        codes = {(d["source"], d["code"]) for d in doc["degraded"]}
        self.assertIn(("manager-rows", "malformed"), codes)
        self.assertIn(("manager-routes", "malformed"), codes)

    def test_a_host_value_not_read_is_none_with_its_reason(self) -> None:
        degraded: list[dict] = []

        def no_load():
            raise AttributeError("getloadavg")
        sample = SM.host_sample(SM._empty_view(None), NOW, degraded, loadavg=no_load,
                                memory=lambda scratch, at: {}, disk_usage=lambda root: (_ for _ in ()).throw(OSError()))
        self.assertEqual((sample["load_per_core"], sample["memory_pressure"], sample["free_disk_gb"]),
                         (None, None, None))
        self.assertEqual({d["code"] for d in degraded}, {"load-unavailable", "pressure-unknown", "disk-unreadable"})


class Feed(unittest.TestCase):
    def write(self, home: Home, doc) -> None:
        home.feed.parent.mkdir(parents=True, exist_ok=True)
        home.feed.write_text(json.dumps(doc), encoding="utf-8")

    FEED = {"observed_at": STAMP - 30,
            "sessions": [{"pid": 102, "pool": "team-a", "state": "working", "quiet_minutes": 3},
                         {"pid": 105, "pool": "team-c", "state": "working", "quiet_minutes": 50}],
            "host": {"at": STAMP - 30, "load_per_core": 2.5, "memory_pressure": "critical", "swap_gb": 9.5,
                     "free_disk_gb": 30.0, "capacity": {"cap": 40, "used": 39, "board_share": 10, "board_used": 9},
                     "groups": {"team-a": {"accounts": 2, "with_room": 0, "unknown": 0}}}}

    def test_a_fresh_feed_is_read_instead_of_the_cli(self) -> None:
        home = Home()
        self.write(home, self.FEED)
        runner = Runner()
        doc = home.summary(runner)
        self.assertEqual([" ".join(a[1:]) for a, _ in runner.calls], ["sessions --machine --json"])
        self.assertEqual(doc["monitor"]["source"], "feed")
        self.assertEqual((doc["host"]["load_per_core"], doc["host"]["memory_pressure"], doc["host"]["swap_gb"]),
                         (2.5, "critical", 9.5))
        self.assertEqual((doc["host"]["sessions_used"], doc["host"]["board_used"]), (39, 9))
        s = by_pid(doc)
        self.assertEqual((s[102]["managed"], s[102]["account_group"]), (True, "team-a"))
        types = {f["type"] for f in CSF.monitor_findings(doc)}
        self.assertEqual(types, {"host.overloaded", "pool.no_room", "session.stalled"})

    def test_fields_the_feed_does_not_carry_are_named(self) -> None:
        home = Home()
        self.write(home, {"observed_at": STAMP, "records": [{"kind": "session", "pid": 102},
                                                             {"kind": "host", "at": STAMP, "load_per_core": 0.5}]})
        doc = home.summary()
        gap = next(d for d in doc["degraded"] if d["code"] == "missing-fields")
        for name in ("session.group", "session.state", "host.memory_pressure", "host.capacity"):
            self.assertIn(name, gap["reason"])
        self.assertEqual(doc["host"]["load_per_core"], 0.5)
        self.assertEqual(doc["host"]["memory_pressure"], "normal", "a value the feed lacks is measured here")

    def test_a_stale_or_unreadable_feed_falls_back_to_the_cli(self) -> None:
        home = Home()
        self.write(home, {**self.FEED, "observed_at": STAMP - 3600})
        runner = Runner()
        doc = home.summary(runner)
        self.assertEqual(doc["monitor"]["source"], "cli")
        self.assertIn(("feed", "stale"), {(d["source"], d["code"]) for d in doc["degraded"]})
        self.assertEqual(len(runner.calls), 5)
        if os.name != "nt":
            other = Home()
            target = other.home / "elsewhere.json"
            target.write_text(json.dumps(self.FEED), encoding="utf-8")
            other.feed.parent.mkdir(parents=True)
            os.symlink(target, other.feed)
            doc = other.summary()
            self.assertIn(("feed", "unreadable"), {(d["source"], d["code"]) for d in doc["degraded"]})
            self.assertEqual(doc["monitor"]["source"], "cli")

    def test_an_absent_feed_is_not_a_gap(self) -> None:
        doc = Home().summary()
        self.assertFalse([d for d in doc["degraded"] if d["source"] == "feed"])

    def test_where_the_feed_is_looked_for(self) -> None:
        home = pathlib.Path("/home/user")
        self.assertEqual(SM.feed_path({SM.FEED_ENV: "/tmp/f.json"}, home), pathlib.Path("/tmp/f.json"))
        self.assertEqual(SM.feed_path({}, home, "darwin"),
                         home / "Library" / "Application Support" / SM.MANAGER_DATA / SM.FEED)
        self.assertEqual(SM.feed_path({}, home, "linux"), home / ".local" / "share" / SM.MANAGER_DATA / SM.FEED)


class Room(unittest.TestCase):
    def test_a_group_has_room_when_one_account_is_under_every_window(self) -> None:
        g = SM.groups_with_room(ACCOUNTS, NOW)
        self.assertEqual(set(g), {"team-a", "team-b"}, "an account moved to another machine serves nothing here")
        self.assertEqual((g["team-a"]["with_room"], g["team-b"]["with_room"]), (1, 0))
        self.assertEqual(g["team-b"]["next_reset_at"], "2026-11-18T13:00:00Z")

    def test_usage_not_known_is_unknown_never_full(self) -> None:
        g = SM.groups_with_room([account("x", "team-d", (100.0, 100.0), at=STAMP - 7200),
                                 {**account("y", "team-d", (1.0, 1.0)), "usage": None}], NOW)
        self.assertEqual((g["team-d"]["with_room"], g["team-d"]["unknown"]), (0, 2))
        doc = {"groups": g}
        self.assertEqual(CSF.monitor_findings(doc), [])


class History(unittest.TestCase):
    def test_one_sample_a_tick_bounded_with_each_session(self) -> None:
        home = Home()
        path = home.home / "store" / SM.SERIES  # paths-check: allow: a temporary workspace home
        doc = home.summary()
        SM.record(doc, path)
        old = {**doc, "measuredAt": "2026-11-15T12:00:00Z", "host": {**doc["host"], "at": "2026-11-15T12:00:00Z"}}
        SM.record(old, path)
        series = SM.record(doc, path)
        self.assertEqual(len(series["host"]), 2, "a sample older than KEEP_HOURS is dropped")
        self.assertEqual(series["host"][0]["load_per_core"], 1.5)
        self.assertEqual(series["host"][0]["groups_with_room"], ["team-a"])
        self.assertEqual(series["sessions"]["launch-2"][-1][1:], ["working", "2026-11-18T12:00:00Z", "team-b"])
        self.assertIn("pid:102", series["sessions"])
        said = path.read_text(encoding="utf-8")
        for private in (LABEL_A, "/srv/example", "example.com"):
            self.assertNotIn(private, said)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_the_number_of_sessions_kept_is_bounded(self) -> None:
        home = Home()
        path = home.home / "store" / SM.SERIES  # paths-check: allow: a temporary workspace home
        rows = [{"pid": i, "launch_id": None, "manager_state": "working", "last_turn_at": None,
                 "account_group": None} for i in range(1, SM.KEEP_SESSIONS + 30)]
        series = SM.record({"measuredAt": "2026-11-18T12:00:00Z", "sessions": rows}, path)
        self.assertEqual(len(series["sessions"]), SM.KEEP_SESSIONS)

    def test_an_unreadable_history_reads_empty(self) -> None:
        home = Home()
        path = home.home / "bad.json"
        path.write_text("{", encoding="utf-8")
        self.assertEqual(SM.read_series(path), {"host": [], "sessions": {}})


class Findings(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = Home().summary()
        self.rows = {f["type"]: f for f in CSF.monitor_findings(self.doc)}

    def test_each_finding_has_an_owner_a_fix_and_a_stable_subject(self) -> None:
        self.assertEqual(set(self.rows), {"pool.no_room", "session.wrong_group", "session.stalled"})
        self.assertEqual(self.rows["pool.no_room"]["subject"], "pool:team-b")
        self.assertEqual(self.rows["session.wrong_group"]["subject"], "session:launch-2")
        self.assertEqual(self.rows["session.stalled"]["subject"], "session:pid:105")
        for f in self.rows.values():
            self.assertIn(f["owner"], (CSF.OWNER_OPERATOR, CSF.OWNER_MANAGER))
            self.assertIn(f["severity"], ("warning", "critical"), "info is never sent to the operator")
            self.assertTrue(f["action"] and f["title_id"])
            self.assertNotIn("\u2014", json.dumps(f, ensure_ascii=False))
        self.assertIn("2026-11-18T13:00:00Z", self.rows["pool.no_room"]["detail"])

    def test_a_critical_host_by_the_settings(self) -> None:
        doc = {"host": {"load_per_core": 2.4, "memory_pressure": "critical"}}
        f = CSF.monitor_findings(doc)[0]
        self.assertEqual((f["type"], f["severity"]), ("host.overloaded", "critical"))
        self.assertEqual(CSF.monitor_findings({"host": {"load_per_core": 2.4, "memory_pressure": "normal"}})[0]["severity"],
                         "warning")
        self.assertEqual(CSF.monitor_findings({"host": {"load_per_core": 2.4, "memory_pressure": "normal"}},
                                     {"sessions_load_per_core_critical": 3}), [])
        self.assertEqual(CSF.monitor_findings({"host": {"load_per_core": None, "memory_pressure": None}}), [])

    def test_the_stall_line_is_never_under_the_managers_quiet_line(self) -> None:
        self.assertEqual(SM.settings({"sessions_stall_minutes": 5})["sessions_stall_minutes"], 30)
        self.assertEqual(SM.settings({"sessions_stall_minutes": 90})["sessions_stall_minutes"], 90)
        self.assertEqual({f["type"] for f in CSF.monitor_findings(self.doc, {"sessions_stall_minutes": 90})},
                         {"pool.no_room", "session.wrong_group"})

    def test_a_quiet_session_is_not_stalled(self) -> None:
        doc = {"sessions": [{"pid": 1, "agent": "claude-code", "busy": False, "quiet_minutes": 300}]}
        self.assertEqual(CSF.monitor_findings(doc), [])

    def test_the_types_have_labels_and_titles_in_both_languages(self) -> None:
        import finding_types
        import i18n
        for kind in ("host.overloaded", "pool.no_room", "session.wrong_group", "session.stalled"):
            self.assertIn(kind, finding_types.LABELS)
            self.assertIn(finding_types.LABELS[kind], i18n.catalog("ru"))
        titles = {"Host overloaded: load per core and memory pressure over their lines",
                  "Host overloaded: load per core over its line", "Host overloaded: memory pressure over its line",
                  "Group {group} has no account with room", "A session in group {own} is served by group {served}",
                  "A {agent} session in {where} is busy with no turn"}
        for msgid in titles:
            self.assertIn(msgid, i18n.catalog("ru"))

    def test_the_findings_reach_the_board_and_the_notify_path(self) -> None:
        import coding_session_findings
        import notify_findings
        rows = coding_session_findings.findings(self.doc)
        types = {f["type"] for f in rows}
        self.assertTrue({"session.unmanaged", "pool.no_room", "session.wrong_group", "session.stalled"} <= types)
        unmanaged = next(f for f in rows if f["type"] == "session.unmanaged")
        self.assertEqual(unmanaged["severity"], "warning", "a session outside the manager is sent once")
        self.assertEqual(unmanaged["owner"], CSF.OWNER_OPERATOR)
        self.assertFalse(types & notify_findings.NOT_PUSHED)


class Page(unittest.TestCase):
    def render(self, lang: str) -> str:
        import agents_page
        doc = Home().summary()
        return agents_page.agents_html({"agents": {}, "sessions": doc}, Translator(lang))

    def test_the_host_line_and_the_findings_in_both_languages(self) -> None:
        en, ru = self.render("en"), self.render("ru")
        self.assertIn('id="agents-host"', en)
        self.assertIn('id="agents-watch"', en)
        self.assertIn("Group team-b has no account with room", en)
        self.assertIn("1.5", en)
        self.assertIn("team-a", en)
        self.assertIn("Группа team-b", ru)
        self.assertIn("менеджер аккаунтов", ru.lower())
        for page in (en, ru):
            self.assertNotIn("\u2014", page)
            self.assertNotIn("switchboard launch", page, "no shell command on a page row")

    def test_a_summary_without_the_monitor_shows_no_host_line(self) -> None:
        import agents_page
        page = agents_page.agents_html({"agents": {}, "sessions": {"sessions": []}}, Translator("en"))
        self.assertNotIn('id="agents-host"', page)


if __name__ == "__main__":
    unittest.main()
