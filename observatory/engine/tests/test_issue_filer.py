#!/usr/bin/env python3
"""The issue filer (OBS-557, CP-14): findings of the allowed classes become issues in the project's own repository,
once, and close when the finding closes.

The code host is a fake that records every call, so nothing reaches a network. Every secret-looking value is made up,
and the bodies, the map and the record are checked to hold none of it.
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "dashboard"))
import tmp as tmpdir                                                # noqa: E402
import issue_filer as f                                             # noqa: E402

TODAY = "2026-11-16"
KEY_LIKE = "-".join(("sk", "or", "v1")) + "-" + "0123456789abcdef" * 4


class FakeHost:
    """A code host that remembers its issues, counts its writes and can refuse."""

    def __init__(self, writable=("example-org/alpha-web", "example-org/beta-api"), existing=None):
        self.writable = set(writable)
        self.issues: dict[str, dict[int, dict]] = {}
        self.calls: list[tuple] = []
        self.next = 1
        for repo, rows in (existing or {}).items():
            for number, row in rows.items():
                self.issues.setdefault(repo, {})[number] = dict(row)
        self.fail_after: int | None = None

    def can_write(self, repo):
        self.calls.append(("can_write", repo))
        return (True, None) if repo in self.writable else (False, "no push access")

    def marked(self, repo):
        self.calls.append(("marked", repo))
        out = {}
        for n, row in self.issues.get(repo, {}).items():
            m = f.MARKER_RE.search(row["body"])
            if m:
                out[m.group(1)] = (n, row["state"])
        return out

    def _write(self, kind):
        writes = sum(1 for c in self.calls if c[0] in ("create", "comment", "close"))
        if self.fail_after is not None and writes >= self.fail_after:
            raise f.HostError(f"rate limited ({kind})", status=403, retry_after=60)

    def create(self, repo, title, body, labels):
        self._write("create")
        n, self.next = self.next, self.next + 1
        self.issues.setdefault(repo, {})[n] = {"title": title, "body": body, "labels": list(labels), "state": "open",
                                               "comments": []}
        self.calls.append(("create", repo, n))
        return n

    def comment(self, repo, number, text):
        self._write("comment")
        self.issues[repo][number]["comments"].append(text)
        self.calls.append(("comment", repo, number))

    def close(self, repo, number):
        self._write("close")
        self.issues[repo][number]["state"] = "closed"
        self.calls.append(("close", repo, number))

    def writes(self):
        return [c for c in self.calls if c[0] in ("create", "comment", "close")]


def vuln(project="project:alpha-web", package="lodash", version="4.17.0"):
    return {"type": "dep.vulnerable", "subject": f"dep:alpha-web/npm/{package}@{version}", "severity": "warning",
            "title": f"{package} {version} has 2 known vulnerabilities", "detail": f"{package} {version} matches X.",
            "action": f"upgrade {package} to 4.17.21", "evidence": ["registry:dependencies.json"],
            "project": project, "package": package, "version": version, "fixed": "4.17.21"}


def untracked(project="project:alpha-web", provider="stripe"):
    return {"type": "integration.untracked", "subject": f"integration:untracked/{project}/{provider}",
            "severity": "info", "title": f"{project} uses {provider}", "detail": "STRIPE_API_KEY, stripe.",
            "action": "add a door", "evidence": ["store/raw/integration-health.json"], "project": project,
            "provider": provider}


def key_invalid(slot="cloudflare/admin/old", project=None):
    row = {"type": "integration.key_invalid", "subject": f"integration:key/{slot}", "severity": "warning",
           "title": f"The key {slot} is refused", "detail": "answered 401", "action": "replace it",
           "evidence": ["store/raw/integration-health.json"]}
    if project:
        row["project"] = project
    return row


REPOS = {"project:alpha-web": {"name_with_owner": "example-org/alpha-web", "visibility": "private", "host": "github.com"},
         "project:beta-api": {"name_with_owner": "example-org/beta-api", "visibility": "public", "host": "github.com"}}


def state_file():
    return pathlib.Path(tmpdir.mkdtemp(prefix="observatory-issues-")) / "issues.json"


def go(findings, host, path=None, settings=None, today=TODAY, cap=10, repos=REPOS):
    path = path or state_file()
    record = f.run(findings, repos, host, path, settings=settings, today=today, cap=cap, sleep=lambda s: None)
    return record, path


class Filing(unittest.TestCase):
    def test_a_vulnerable_dependency_becomes_one_issue_with_a_marker(self):
        host = FakeHost()
        record, path = go([vuln()], host)
        issue = host.issues["example-org/alpha-web"][1]
        self.assertEqual(record["created"], 1)
        self.assertIn("lodash 4.17.0", issue["title"])
        self.assertTrue(f.MARKER_RE.search(issue["body"]))
        self.assertIn(f.LABEL, issue["labels"])
        entry = next(iter(json.loads(path.read_text())["issues"].values()))
        self.assertEqual((entry["repo"], entry["number"], entry["state"]), ("example-org/alpha-web", 1, "open"))

    def test_the_same_finding_twice_is_one_issue(self):
        host, path = FakeHost(), state_file()
        go([vuln()], host, path)
        go([vuln()], host, path, today="2026-11-17")
        self.assertEqual(len(host.writes()), 1)

    def test_the_local_map_lost_but_the_marker_found_files_nothing_new(self):
        host = FakeHost()
        go([vuln()], host)                      # the first run's map is another file: lost
        record, path = go([vuln()], host)
        self.assertEqual(len(host.issues["example-org/alpha-web"]), 1)
        self.assertEqual(record["created"], 0)
        entry = next(iter(json.loads(path.read_text())["issues"].values()))
        self.assertEqual((entry["number"], entry["state"]), (1, "open"))

    def test_writes_are_serial_and_capped_and_the_rest_waits_for_the_next_run(self):
        host, path = FakeHost(), state_file()
        found = [vuln(package=f"pkg{i}") for i in range(7)]
        sleeps: list[float] = []
        record = f.run(found, REPOS, host, path, today=TODAY, cap=3, sleep=sleeps.append)
        self.assertEqual((record["created"], len(host.writes())), (3, 3))
        self.assertEqual(record["deferred"], 4)
        self.assertTrue(all(s >= 1.0 for s in sleeps) and len(sleeps) >= 2, sleeps)
        f.run(found, REPOS, host, path, today="2026-11-17", cap=3, sleep=lambda s: None)
        self.assertEqual(len(host.writes()), 6)

    def test_a_rate_limit_stops_the_run_and_is_recorded(self):
        host = FakeHost()
        host.fail_after = 1
        record, _ = go([vuln(package="a"), vuln(package="b")], host)
        self.assertEqual(record["created"], 1)
        self.assertEqual(record["stopped"]["reason"], "rate-limited")
        self.assertEqual(record["stopped"]["retry_after"], 60)

    def test_a_finding_closed_comments_and_closes_its_issue_once(self):
        host, path = FakeHost(), state_file()
        go([vuln(), vuln(package="other")], host, path)
        record, _ = go([vuln()], host, path, today="2026-11-20")
        self.assertEqual(record["closed"], 1)
        closed = [i for i in host.issues["example-org/alpha-web"].values() if i["state"] == "closed"]
        self.assertEqual(len(closed), 1)
        self.assertIn("2026-11-20", closed[0]["comments"][0])
        go([vuln()], host, path, today="2026-11-21")
        self.assertEqual(len([c for c in host.calls if c[0] == "close"]), 1)
        entry = [e for e in json.loads(path.read_text())["issues"].values() if e["state"] == "closed"][0]
        self.assertEqual(entry["closed_on"], "2026-11-20")

    def test_a_recurring_finding_after_its_issue_closed_files_a_new_one(self):
        host, path = FakeHost(), state_file()
        go([vuln()], host, path)
        go([], host, path, today="2026-11-18")
        go([vuln()], host, path, today="2026-11-19")
        self.assertEqual(len(host.issues["example-org/alpha-web"]), 2)


class Rules(unittest.TestCase):
    def test_no_write_access_is_recorded_not_filed(self):
        host = FakeHost(writable=())
        record, path = go([vuln()], host)
        self.assertEqual((record["created"], host.writes()), (0, []))
        entry = next(iter(json.loads(path.read_text())["issues"].values()))
        self.assertEqual(entry["state"], "not-filed")
        self.assertIn("no push access", entry["reason"])

    def test_a_finding_with_no_project_or_no_repository_is_not_filed(self):
        host = FakeHost()
        record, path = go([key_invalid(), vuln(project="project:gamma")], host)
        self.assertEqual(host.writes(), [])
        reasons = {e["reason"] for e in json.loads(path.read_text())["issues"].values()}
        self.assertTrue(any("no project" in r for r in reasons) and any("no repository" in r for r in reasons), reasons)

    def test_key_findings_are_never_filed_into_a_public_repository(self):
        host = FakeHost()
        settings = {"integrations": {"github_issues_keys": True}}
        found = [key_invalid(project="project:beta-api"),
                 {"type": "credential.expired", "subject": "credential:vault/beta-api/X", "severity": "warning",
                  "title": "Key expired", "detail": "d", "action": "a", "evidence": [], "project": "project:beta-api"}]
        record, path = go(found, host, settings=settings)
        self.assertEqual(host.writes(), [])
        self.assertTrue(all("public" in e["reason"] for e in json.loads(path.read_text())["issues"].values()))
        record, _ = go([key_invalid(project="project:alpha-web")], host, settings=settings)
        self.assertEqual(record["created"], 1, "the same finding in a private repository is filed")

    def test_the_default_classes_and_the_switches(self):
        host = FakeHost()
        go([vuln(), untracked(), {"type": "credential.expired", "subject": "credential:vault/alpha-web/X",
                                  "severity": "warning", "title": "t", "detail": "d", "action": "a", "evidence": [],
                                  "project": "project:alpha-web"}], host)
        types = sorted(i["title"].split()[0] for i in host.issues["example-org/alpha-web"].values())
        self.assertEqual(len(types), 2, "dependency and integration findings by default; key findings are not")
        host2 = FakeHost()
        go([vuln(), untracked()], host2, settings={"integrations": {"github_issues_dependencies": False}})
        self.assertEqual(len(host2.writes()), 1)
        host3 = FakeHost()
        record, _ = go([vuln(), untracked()], host3, settings={"integrations": {"github_issues": False}})
        self.assertEqual((host3.calls, record["enabled"]), ([], False))

    def test_the_master_switch_and_offline_ask_nothing(self):
        host = FakeHost()
        record = f.run([vuln()], REPOS, host, state_file(), settings={"integrations": {"github_issues": False}},
                       today=TODAY, sleep=lambda s: None)
        self.assertFalse(record["enabled"])
        record = f.run([vuln()], REPOS, host, state_file(), today=TODAY, offline=True, sleep=lambda s: None)
        self.assertFalse(record["enabled"])
        self.assertEqual(host.calls, [])

    def test_a_dry_run_plans_and_writes_nothing(self):
        record = f.run([vuln(), untracked()], REPOS, None, state_file(), today=TODAY, sleep=lambda s: None)
        self.assertTrue(record["dry_run"])
        self.assertEqual(record["created"], 0)
        self.assertEqual(len(record["planned"]), 2)


class NoValues(unittest.TestCase):
    def test_a_body_holds_no_value_and_a_credential_shaped_text_refuses_the_issue(self):
        host = FakeHost()
        bad = vuln()
        bad["detail"] = f"found {KEY_LIKE} in the lockfile"
        record, path = go([vuln(package="clean"), bad], host)
        self.assertEqual(record["created"], 1)
        blob = json.dumps(host.issues) + path.read_text() + json.dumps(record)
        self.assertNotIn(KEY_LIKE, blob)
        self.assertTrue(any("credential" in e["reason"] for e in json.loads(path.read_text())["issues"].values()
                            if e["state"] == "not-filed"))

    def test_body_text_says_how_to_turn_it_off_and_names_no_workspace_path(self):
        host = FakeHost()
        go([untracked()], host)
        body = host.issues["example-org/alpha-web"][1]["body"]
        self.assertIn("github_issues", body)
        self.assertNotIn("/Users/", body)
        self.assertNotIn(chr(0x2014), body)


class Gh(unittest.TestCase):
    def test_the_gh_host_sends_the_body_on_stdin_never_in_argv(self):
        calls = []

        def runner(args, stdin=None):
            calls.append((list(args), stdin))
            return 0, json.dumps({"number": 7}), ""
        host = f.GhHost(runner)
        n = host.create("example-org/alpha-web", "title", "body text with marker", ["observatory"])
        self.assertEqual(n, 7)
        args, stdin = calls[0]
        self.assertNotIn("body text with marker", " ".join(args))
        self.assertIn("body text with marker", stdin)
        self.assertIn("--input", args)

    def test_a_refusal_carries_the_status_and_retry_after(self):
        def runner(args, stdin=None):
            return 1, "", "gh: You have exceeded a secondary rate limit (HTTP 403)\nretry-after: 45"
        with self.assertRaises(f.HostError) as cm:
            f.GhHost(runner).create("example-org/alpha-web", "t", "b", [])
        self.assertEqual((cm.exception.status, cm.exception.retry_after), (403, 45))

    def test_can_write_reads_the_permission_and_has_issues(self):
        out = {"permissions": {"push": True}, "has_issues": True}
        host = f.GhHost(lambda a, s=None: (0, json.dumps(out), ""))
        self.assertEqual(host.can_write("example-org/alpha-web"), (True, None))
        out2 = {"permissions": {"push": False}, "has_issues": True}
        self.assertEqual(f.GhHost(lambda a, s=None: (0, json.dumps(out2), "")).can_write("x/y")[0], False)
        out3 = {"permissions": {"push": True}, "has_issues": False}
        self.assertIn("issues", f.GhHost(lambda a, s=None: (0, json.dumps(out3), "")).can_write("x/y")[1])


class Banner(unittest.TestCase):
    def test_shown_until_dismissed_and_the_dismissal_is_stored(self):
        path = state_file()
        self.assertTrue(f.banner(path, settings=None)["show"])
        f.dismiss_banner(path, today=TODAY)
        b = f.banner(path, settings=None)
        self.assertEqual((b["show"], b["dismissed_on"]), (False, TODAY))
        go([vuln()], FakeHost(), path)
        self.assertEqual(f.banner(path, settings=None)["dismissed_on"], TODAY, "a run keeps the dismissal")

    def test_not_shown_while_the_filer_is_off(self):
        self.assertFalse(f.banner(state_file(), settings={"integrations": {"github_issues": False}})["show"])


class AgentView(unittest.TestCase):
    def test_the_view_names_the_map_the_last_run_and_why_when_nothing_ran(self):
        v = f.agent_view(path=state_file())
        self.assertEqual((v["issues"], v["lastRun"]), ([], None))
        self.assertTrue(v["degraded"])
        host, path = FakeHost(), state_file()
        go([vuln(), untracked(provider="paddle")], host, path)
        v = f.agent_view(path=path)
        self.assertEqual(len(v["issues"]), 2)
        self.assertEqual(v["lastRun"]["created"], 2)
        self.assertEqual(v["degraded"], [])
        self.assertEqual({r["state"] for r in f.agent_view(path=path, state="open")["issues"]}, {"open"})


if __name__ == "__main__":
    unittest.main()
