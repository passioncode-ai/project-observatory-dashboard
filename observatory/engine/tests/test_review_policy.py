#!/usr/bin/env python3
"""The standing review policy (OBS-227): decided once by a person, applied by the tick, never as them.

The operator asked, on 2026-10-24, that the whole queue of proposals be reviewed constantly and never
forgotten, preferably without a coding agent, and that what needs a person accumulates so one launch
resolves it. These cases hold the boundary that request must not move: a model may only propose
(rule 3), so nothing here writes `owner = operator` or `decided_by = operator` without a person at a
terminal, every policy decision names its rule and the hash a person enabled, and every one can be
taken back. The decision record is docs/design/REVIEW-POLICY.md.

Self-contained: one synthetic workspace (`project:example-app`, `project:example-tool`), reset before
every case; the terminal is a pseudo-terminal, never the person's.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import tmp as tmpdir                                                                # noqa: E402

BASE = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-review-policy-"))
HOME = BASE / "home"
os.environ.update({
    "OBSERVATORY_HOME": str(HOME), "OBSERVATORY_REGISTRY": str(HOME / "registry"),
    "OBSERVATORY_DB": str(HOME / "store/observatory.db"), "OBSERVATORY_STATE": str(HOME / "store"),
    "OBSERVATORY_SCRATCH": str(HOME / "store/raw")})

import paths                                                                        # noqa: E402
import review_policy as RP                                                          # noqa: E402
from store import db as store_db                                                    # noqa: E402
from store import ledger as L                                                       # noqa: E402

PY = sys.executable
REVIEW = ROOT / "tools" / "review.py"
APP, TOOL = "project:example-app", "project:example-tool"
AGENT = "agent:example-agent"
EVIDENCE = [{"uri": "https://example.invalid/receipt/1"}]


def stamp(days_ago: float = 0, hour: int = 12) -> str:
    at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return at.replace(hour=hour, minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def reset() -> None:
    shutil.rmtree(HOME, ignore_errors=True)
    (HOME / "config").mkdir(parents=True)
    (HOME / "registry").mkdir(parents=True)
    (HOME / "store/raw").mkdir(parents=True)
    write("registry/projects.json", {"projects": [
        {"id": APP, "name": "Example app", "description": "An example application"},
        {"id": TOOL, "name": "Example tool"}]})
    write("registry/products.json", {"products": []})
    write("registry/relations.json", {"relations": [
        {"type": "implemented_by", "from": TOOL, "to": "repository:example-org/example-tool"}]})
    write("config/project_overrides.json", {"projects": {
        APP: {"description": "An example application", "why": "a person wrote this",
              "resources": [{"kind": "server", "identifier": "app.example.invalid"}]}}})
    write("config/identifiers.json", {"schema_version": 1, "aliases": {
        "store:example-1": {"resolves_to": TOOL, "owner": "agent:store", "source": "a store console",
                            "since": "2026-10-01"}}})
    store_db.connect().close()


def write(rel: str, doc) -> None:
    (HOME / rel).write_text(json.dumps(doc), encoding="utf-8")


def read(rel: str):
    return json.loads((HOME / rel).read_text(encoding="utf-8"))


def connect() -> sqlite3.Connection:
    conn = store_db.connect()
    conn.row_factory = sqlite3.Row
    return conn


def propose(patch: dict, target: str = APP, owner: str = AGENT, created: str | None = None,
            evidence=EVIDENCE) -> str:
    conn = connect()
    try:
        pid = L.proposals_add(conn, target_id=target, patch=patch, evidence=evidence, owner=owner)["proposalId"]
        if created:
            with conn:
                conn.execute("UPDATE proposals SET created_at = ? WHERE id = ?", (created, pid))
        return pid
    finally:
        conn.close()


def conclusion(mid: str, *, project: str = APP, kind: str = "observation", owner: str = "agent:observer",
               created: str | None = None, delta: str = "owner-changed") -> None:
    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO ledger (memory_id, revision, kind, project_id, owner, function, scope, statement,"
            " state, confidence, created_at, evidence_json) VALUES (?,1,?,?,?,'episodic','project',?,"
            " 'proposed',0.5,?,?)",
            (mid, kind, project, owner, f"a reading of {project}", created or stamp(),
             json.dumps([{"kind": delta}])))
    conn.close()


def proposal(pid: str) -> dict:
    conn = connect()
    try:
        return dict(conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone())
    finally:
        conn.close()


def enable(only=None) -> None:
    doc = RP.enable_document(RP.read_document(), only=only, at=RP.now(), user="tester")
    write("config/review-policy.json", doc)


def run_policy(**kw) -> dict:
    conn = connect()
    try:
        return RP.run(conn, **kw)
    finally:
        conn.close()


def cli(*args: str, terminal: bool = False, answer: str = "") -> tuple[int, str]:
    """`review.py ARGS`, its stdin a pipe or a pseudo-terminal that answers `answer`."""
    cmd = [PY, str(REVIEW), *args]
    if not terminal:
        p = subprocess.run(cmd, cwd=ROOT, env=os.environ.copy(), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=120)
        return p.returncode, p.stdout + p.stderr
    import pty
    master, slave = pty.openpty()
    try:
        p = subprocess.Popen(cmd, cwd=ROOT, env=os.environ.copy(), stdin=slave, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        if answer:
            os.write(master, answer.encode())
        out, _ = p.communicate(timeout=120)
        return p.returncode, out
    finally:
        os.close(master)
        os.close(slave)


def operator_writes() -> int:
    """Rows any review write attributed to the operator: proposals and ledger revisions."""
    conn = connect()
    try:
        return (conn.execute("SELECT COUNT(*) FROM proposals WHERE decided_by = 'operator'").fetchone()[0]
                + conn.execute("SELECT COUNT(*) FROM ledger WHERE owner = 'operator'").fetchone()[0])
    finally:
        conn.close()


class Case(unittest.TestCase):
    def setUp(self):
        reset()


class ThePolicyFile(Case):
    def test_no_file_is_off_and_decides_nothing_but_counts_what_it_would(self):
        pid = propose({"resources": [{"kind": "ga4-property", "identifier": "properties/1"}]})
        receipt = run_policy()
        self.assertEqual(receipt["state"], "off")
        self.assertEqual(receipt["applied"], {})
        self.assertEqual(receipt["would"], {"registry.resources-append": 1})
        self.assertEqual(proposal(pid)["status"], "proposed")
        self.assertEqual(receipt["waiting"]["registry"], 1, "off: what a rule would decide waits for a person")
        self.assertEqual(read("store/raw/review-policy.json")["would"], receipt["would"])

    def test_an_unknown_rule_makes_the_whole_policy_invalid(self):
        doc = RP.default_document()
        doc["rules"]["registry.accept-everything"] = {"version": 1, "on": True}
        write("config/review-policy.json", doc)
        pol = RP.load()
        self.assertEqual(pol.state, "invalid")
        self.assertIn("unknown rule", pol.problem)
        self.assertEqual(pol.rules_on(), set())
        propose({"resources": [{"kind": "server", "identifier": "new.example.invalid"}]})
        self.assertEqual(run_policy()["applied"], {})

    def test_changing_the_rules_after_enabling_stops_the_policy(self):
        enable()
        self.assertEqual(RP.load().state, "on")
        doc = read("config/review-policy.json")
        doc["rules"]["registry.exact-duplicate"]["on"] = False
        write("config/review-policy.json", doc)
        pol = RP.load()
        self.assertEqual(pol.state, "changed")
        pid = propose({"resources": [{"kind": "server", "identifier": "new.example.invalid"}]})
        self.assertEqual(run_policy()["applied"], {})
        self.assertEqual(proposal(pid)["status"], "proposed")

    def test_a_rule_version_the_engine_no_longer_has_needs_enabling_again(self):
        enable()
        doc = read("config/review-policy.json")
        doc["rules"]["registry.resources-append"]["version"] = 0
        doc["enabled"]["hash"] = RP.rules_hash(doc["rules"])     # even a forged hash
        write("config/review-policy.json", doc)
        self.assertEqual(RP.load().state, "changed")

    def test_enable_and_disable_need_a_terminal(self):
        code, out = cli("policy", "enable")
        self.assertNotEqual(code, 0)
        self.assertIn("terminal", out)
        self.assertFalse((HOME / "config/review-policy.json").exists())
        code, out = cli("policy", "enable", terminal=True, answer="y\n")
        self.assertEqual(code, 0, out)
        doc = read("config/review-policy.json")
        self.assertEqual(doc["enabled"]["hash"], RP.rules_hash(doc["rules"]))
        self.assertEqual(doc["enabled"]["by"], "operator at a terminal")
        self.assertEqual(RP.load().state, "on")
        code, out = cli("policy", "disable")
        self.assertNotEqual(code, 0)
        code, out = cli("policy", "disable", terminal=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(RP.load().state, "off")
        self.assertEqual([h["event"] for h in read("config/review-policy.json")["history"]], ["enabled", "disabled"])

    def test_a_refused_confirmation_writes_nothing(self):
        code, out = cli("policy", "enable", terminal=True, answer="n\n")
        self.assertNotEqual(code, 0)
        self.assertFalse((HOME / "config/review-policy.json").exists())

    def test_show_reads_and_writes_nothing(self):
        pid = propose({"resources": [{"kind": "server", "identifier": "new.example.invalid"}]})
        code, out = cli("policy", "show")
        self.assertEqual(code, 0, out)
        self.assertIn("registry.resources-append", out)
        self.assertIn("off", out)
        self.assertIn(pid, out, "a dry run names an example of what each rule would decide")
        self.assertFalse((HOME / "config/review-policy.json").exists())
        self.assertEqual(proposal(pid)["status"], "proposed")


class RegistryRules(Case):
    def setUp(self):
        super().setUp()
        enable()

    def test_a_resources_patch_is_appended_and_decided_by_the_policy_never_the_operator(self):
        pid = propose({"resources": [{"kind": "ga4-property", "identifier": "properties/1"},
                                     {"kind": "server", "identifier": "app.example.invalid"}]})
        receipt = run_policy()
        self.assertEqual(receipt["applied"], {"registry.resources-append": 1})
        row = proposal(pid)
        self.assertEqual(row["status"], "accepted")
        self.assertRegex(row["decided_by"], r"^policy:registry\.resources-append@[0-9a-f]{12}$")
        self.assertIn("1 already recorded", row["decided_note"])
        over = read("config/project_overrides.json")["projects"][APP]
        self.assertEqual(sorted(r["identifier"] for r in over["resources"]),
                         ["app.example.invalid", "properties/1"])
        self.assertEqual(over["why"], "a person wrote this", "a person's provenance is not overwritten")
        self.assertEqual(operator_writes(), 0)
        journal = RP.active_decisions()
        self.assertEqual([(d["item"], d["decided_by"]) for d in journal], [(pid, row["decided_by"])])

    def test_a_new_row_carries_the_policy_as_its_reason(self):
        pid = propose({"resources": [{"kind": "server", "identifier": "tool.example.invalid"}]}, target=TOOL)
        run_policy()
        row = read("config/project_overrides.json")["projects"][TOOL]
        self.assertTrue(row["why"].startswith("policy:registry.resources-append"))
        self.assertTrue(row["proposed_by"].startswith(pid))

    def test_a_resource_recorded_with_other_fields_waits_for_a_person(self):
        pid = propose({"resources": [{"kind": "server", "identifier": "app.example.invalid",
                                      "note": "moved"}]})
        receipt = run_policy()
        self.assertEqual(receipt["applied"], {})
        self.assertEqual(proposal(pid)["status"], "proposed")
        self.assertEqual(receipt["waiting"]["registry"], 1)

    def test_a_target_outside_the_registry_or_a_non_agent_owner_waits(self):
        a = propose({"resources": [{"kind": "server", "identifier": "x.example.invalid"}]},
                    target="project:not-in-registry")
        b = propose({"resources": [{"kind": "server", "identifier": "y.example.invalid"}]}, owner="someone")
        run_policy()
        self.assertEqual((proposal(a)["status"], proposal(b)["status"]), ("proposed", "proposed"))

    def test_exact_duplicates_are_superseded_and_the_oldest_stays(self):
        first = propose({"description": "A better description"}, created=stamp(3))
        second = propose({"description": "A better description"}, created=stamp(1))
        run_policy()
        self.assertEqual(proposal(first)["status"], "proposed", "a description is a person's")
        row = proposal(second)
        self.assertEqual(row["status"], "superseded")
        self.assertIn(first, row["decided_note"])

    def test_two_proposals_of_one_resource_add_it_once(self):
        a = propose({"resources": [{"kind": "server", "identifier": "n.example.invalid"}]}, created=stamp(2))
        b = propose({"resources": [{"kind": "server", "identifier": "n.example.invalid"},
                                   {"kind": "dns-zone", "identifier": "example.invalid"}]}, created=stamp(1))
        run_policy()
        res = read("config/project_overrides.json")["projects"][APP]["resources"]
        self.assertEqual(sorted(r["identifier"] for r in res),
                         ["app.example.invalid", "example.invalid", "n.example.invalid"])
        self.assertEqual((proposal(a)["status"], proposal(b)["status"]), ("accepted", "accepted"))

    def test_aliases_without_conflict_are_recorded_and_conflicts_wait(self):
        clean = propose({"aliases": [{"id": "store:example-2", "owner": "agent:store", "source": "a store"}]})
        curated = propose({"aliases": [{"id": "store:example-1", "owner": "agent:store", "source": "a store"}]})
        measured = propose({"aliases": [{"id": "repository:example-org/example-tool", "owner": "agent:x",
                                         "source": "a host"}]})
        run_policy()
        self.assertEqual(proposal(clean)["status"], "accepted")
        table = read("config/identifiers.json")["aliases"]
        self.assertEqual(table["store:example-2"]["resolves_to"], APP)
        self.assertTrue(table["store:example-2"]["why"].startswith("policy:registry.aliases-clean"))
        self.assertEqual(proposal(curated)["status"], "proposed", "a name curated for another target")
        self.assertEqual(proposal(measured)["status"], "proposed", "a name measured on another project")

    def test_description_organization_and_local_folders_wait_for_a_person(self):
        ids = [propose({"description": "x"}), propose({"organization": "external"}, target=TOOL)]
        receipt = run_policy()
        self.assertEqual([proposal(i)["status"] for i in ids], ["proposed", "proposed"])
        self.assertEqual(receipt["waiting"]["registry"], 2)

    def test_a_run_is_bounded_and_the_rest_waits_for_the_next(self):
        for n in range(3):
            propose({"resources": [{"kind": "server", "identifier": f"b{n}.example.invalid"}]})
        receipt = run_policy(limit=2)
        self.assertEqual(receipt["applied"], {"registry.resources-append": 2})
        self.assertEqual(receipt["deferred"], 1)
        self.assertEqual(receipt["waiting"]["registry"], 0, "deferred is the next run's, not a person's")
        self.assertEqual(run_policy()["applied"], {"registry.resources-append": 1})

    def test_a_held_lock_skips_the_run_and_says_so(self):
        propose({"resources": [{"kind": "server", "identifier": "l.example.invalid"}]})
        with RP.lock():
            receipt = run_policy()
        self.assertIn("another review write", receipt["skipped"])
        self.assertEqual(receipt["applied"], {})


class TheLedger(Case):
    def setUp(self):
        super().setUp()
        day = stamp(2, hour=8)
        conclusion("mem:a1", created=day)
        conclusion("mem:a2", created=day.replace("T08", "T09"))
        conclusion("mem:a3", created=day.replace("T08", "T10"))
        conclusion("mem:routine", project=TOOL, delta="commits-changed")
        conclusion("mem:lesson", kind="lesson", owner="agent:task-pipeline")
        conclusion("mem:other", project=TOOL, owner="agent:claude-code", kind="note")

    def test_superseded_readings_fold_in_the_journal_and_the_ledger_is_untouched(self):
        enable()
        conn = connect()
        before = conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        conn.close()
        receipt = run_policy()
        self.assertEqual(receipt["applied"], {"ledger.fold-same-day": 2})
        conn = connect()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], before)
        self.assertEqual({r[0] for r in conn.execute("SELECT state FROM ledger")}, {"proposed"})
        conn.close()
        self.assertEqual(receipt["folded"], 2)
        self.assertEqual(receipt["retention"], 1)
        self.assertEqual(receipt["corroboration"], 1)
        self.assertEqual(receipt["waiting"]["ledger"], 2, "the latest reading and the structural note")
        self.assertEqual(run_policy()["applied"], {}, "a fold is decided once")

    def test_off_every_reading_waits(self):
        receipt = run_policy()
        self.assertEqual(receipt["waiting"]["ledger"], 4)

    def test_a_reading_corrected_after_the_fold_is_no_longer_folded(self):
        enable()
        run_policy()
        conn = connect()
        cur = L.current(conn, "mem:a1")
        L.transition(conn, "mem:a1", to_state="proposed", owner="agent:observer",
                     expected_revision=cur["revision"], why="corrected")
        conn.close()
        # The correction is a new revision of today: no longer the folded reading, so a person sees it.
        receipt = run_policy()
        self.assertEqual(receipt["applied"], {})
        self.assertEqual(receipt["folded"], 1)
        self.assertEqual(receipt["waiting"]["ledger"], 3)


class Undo(Case):
    def setUp(self):
        super().setUp()
        enable()

    def test_undo_needs_a_terminal_and_restores_exactly_what_the_decision_added(self):
        pid = propose({"resources": [{"kind": "ga4-property", "identifier": "properties/7"}]})
        run_policy()
        decision = RP.active_decisions()[0]["id"]
        # A person adds a resource afterwards; undo must not take it.
        doc = read("config/project_overrides.json")
        doc["projects"][APP]["resources"].append({"kind": "figma-file", "identifier": "file-1"})
        write("config/project_overrides.json", doc)
        code, out = cli("undo-policy", decision)
        self.assertNotEqual(code, 0)
        self.assertIn("terminal", out)
        code, out = cli("undo-policy", decision, terminal=True)
        self.assertEqual(code, 0, out)
        row = proposal(pid)
        self.assertEqual((row["status"], row["decided_by"]), ("proposed", None))
        res = read("config/project_overrides.json")["projects"][APP]["resources"]
        self.assertEqual(sorted(r["identifier"] for r in res), ["app.example.invalid", "file-1"])
        self.assertEqual(RP.active_decisions(), [])

    def test_undo_since_reverts_a_new_row_and_an_alias_and_a_fold(self):
        propose({"resources": [{"kind": "server", "identifier": "t.example.invalid"}]}, target=TOOL)
        propose({"aliases": [{"id": "store:example-9", "owner": "agent:store", "source": "a store"}]})
        conclusion("mem:b1", created=stamp(1, hour=8))
        conclusion("mem:b2", created=stamp(1, hour=9))
        start = RP.now()
        run_policy()
        self.assertEqual(len(RP.active_decisions()), 3)
        code, out = cli("undo-policy", "--since", start, terminal=True)
        self.assertEqual(code, 0, out)
        self.assertNotIn(TOOL, read("config/project_overrides.json")["projects"])
        self.assertNotIn("store:example-9", read("config/identifiers.json")["aliases"])
        self.assertEqual(RP.active_decisions(), [])
        self.assertEqual(run_policy(apply=False)["would"], {"ledger.fold-same-day": 1,
                                                            "registry.aliases-clean": 1,
                                                            "registry.resources-append": 1})

    def test_a_proposal_decided_again_since_is_left_alone(self):
        pid = propose({"resources": [{"kind": "server", "identifier": "z.example.invalid"}]})
        run_policy()
        conn = connect()
        with conn:
            conn.execute("UPDATE proposals SET status='rejected', decided_by='operator' WHERE id=?", (pid,))
        conn.close()
        code, out = cli("undo-policy", RP.active_decisions()[0]["id"], terminal=True)
        self.assertEqual(code, 0, out)
        self.assertIn("decided again since", out)
        self.assertEqual(proposal(pid)["status"], "rejected")


class Batch(Case):
    def test_prepare_recommends_and_apply_asks_once_at_a_terminal(self):
        first = propose({"description": "The tool"}, target=TOOL)            # none yet: accept
        second = propose({"description": "Another"})                          # replaces: keep
        dup_a = propose({"organization": "external"}, target=TOOL, created=stamp(3))
        dup_b = propose({"organization": "external"}, target=TOOL, created=stamp(2))
        res = propose({"resources": [{"kind": "server", "identifier": "q.example.invalid"}]})
        conclusion("mem:c1", created=stamp(1, hour=8))
        conclusion("mem:c2", created=stamp(1, hour=9))
        out_file = BASE / "batch.json"
        code, out = cli("batch", "prepare", "--out", str(out_file))
        self.assertEqual(code, 0, out)
        doc = json.loads(out_file.read_text())
        by = {it["id"]: it for it in doc["items"]}
        self.assertEqual(by[first]["recommended"], "accept")
        self.assertEqual(by[second]["recommended"], "keep")
        self.assertEqual(by[dup_b]["recommended"], "reject")
        self.assertEqual(by[res]["recommended"], "accept", "the policy is off: the rule's verdict, as advice")
        self.assertEqual(by["mem:c1"]["recommended"], "reject")
        self.assertEqual(by["mem:c2"]["recommended"], "keep")
        self.assertTrue(all(it["hash"] and it["reason"] for it in doc["items"]))
        self.assertEqual(operator_writes(), 0, "preparing decides nothing")

        # An agent edits one decision; the queue moves under another item before the launch.
        by[second]["decision"], by[second]["reason"] = "reject", "the agent read it: not better"
        out_file.write_text(json.dumps(doc))
        conn = connect()
        with conn:
            conn.execute("UPDATE proposals SET patch_json = ? WHERE id = ?",
                         (json.dumps({"organization": "external", "x": 1}), dup_a))
        conn.close()

        code, out = cli("batch", "apply", str(out_file))
        self.assertNotEqual(code, 0)
        self.assertIn("terminal", out)
        code, out = cli("batch", "apply", str(out_file), terminal=True, answer="y\n")
        self.assertEqual(code, 0, out)
        self.assertIn("skipped", out)
        self.assertIn(dup_a, out)
        self.assertEqual(proposal(first)["status"], "accepted")
        self.assertEqual(proposal(first)["decided_by"], "operator")
        self.assertTrue(proposal(first)["decided_note"].startswith("batch "))
        self.assertIn("no description yet", proposal(first)["decided_note"])
        self.assertEqual(proposal(second)["status"], "rejected")
        self.assertIn("the agent read it", proposal(second)["decided_note"])
        self.assertEqual(proposal(dup_a)["status"], "proposed", "changed after preparation: skipped")
        self.assertEqual(proposal(res)["status"], "accepted")
        conn = connect()
        self.assertEqual(L.current(conn, "mem:c1")["state"], "rejected")
        self.assertEqual(L.current(conn, "mem:c1")["owner"], "operator")
        self.assertEqual(L.current(conn, "mem:c2")["state"], "proposed", "keep decides nothing")
        conn.close()

    def test_a_declined_launch_applies_nothing(self):
        pid = propose({"description": "The tool"}, target=TOOL)
        out_file = BASE / "declined.json"
        cli("batch", "prepare", "--out", str(out_file))
        code, _ = cli("batch", "apply", str(out_file), terminal=True, answer="n\n")
        self.assertNotEqual(code, 0)
        self.assertEqual(proposal(pid)["status"], "proposed")

    def test_a_malformed_file_is_refused_whole(self):
        bad = BASE / "bad.json"
        bad.write_text(json.dumps({"schema": "review-batch/1", "items": [{"id": "prop:x", "queue": "registry",
                                                                          "decision": "maybe"}]}))
        code, out = cli("batch", "apply", str(bad), terminal=True, answer="y\n")
        self.assertNotEqual(code, 0)
        self.assertIn("decision", out)

    def test_an_item_listed_twice_refuses_the_batch_whole(self):
        pid = propose({"description": "The tool"}, target=TOOL)
        f = _batch_file("twice.json")
        doc = json.loads(f.read_text())
        doc["items"].append(dict(doc["items"][0]))
        f.write_text(json.dumps(doc))
        code, out = cli("batch", "apply", str(f), terminal=True, answer="y\n")
        self.assertNotEqual(code, 0)
        self.assertIn("twice", out)
        self.assertEqual(proposal(pid)["status"], "proposed")

    def test_folded_readings_reach_the_batch_as_rejections(self):
        enable()
        conclusion("mem:d1", created=stamp(1, hour=8))
        conclusion("mem:d2", created=stamp(1, hour=9))
        run_policy()
        conn = connect()
        try:
            doc = RP.prepare_batch(conn)
        finally:
            conn.close()
        by = {it["id"]: it for it in doc["items"]}
        self.assertEqual(by["mem:d1"]["decision"], "reject")
        self.assertIn("policy:ledger.fold-same-day", by["mem:d1"]["reason"])


class NeverForgotten(Case):
    def findings(self, summary):
        sys.path.insert(0, str(ROOT / "tools"))
        import review_findings
        return review_findings.findings(summary)

    def test_the_finding_speaks_by_count_or_by_age_and_names_the_commands(self):
        conn = connect()
        try:
            quiet = RP.summary(conn)
        finally:
            conn.close()
        self.assertEqual(self.findings(quiet), [])
        propose({"description": "old"}, created=stamp(20))
        conn = connect()
        try:
            old = RP.summary(conn)
        finally:
            conn.close()
        rows = self.findings(old)
        self.assertEqual([r["type"] for r in rows], ["review.queue_waiting"])
        self.assertEqual(rows[0]["severity"], "warning")
        for words in ("review policy show", "review batch prepare", "review batch apply"):
            self.assertIn(words, rows[0]["action"])
        many = {**old, "waiting": {"total": 25, "registry": 5, "ledger": 20, "oldest": stamp(1)}}
        self.assertEqual(len(self.findings(many)), 1)
        few = {**old, "waiting": {"total": 3, "registry": 3, "ledger": 0, "oldest": stamp(1)}}
        self.assertEqual(self.findings(few), [])

    def test_doctor_and_health_read_the_same_summary(self):
        enable()
        propose({"description": "x"})
        run_policy()
        st = RP.status()
        self.assertEqual(st["policy"]["state"], "on")
        self.assertEqual(st["waiting"]["registry"], 1)
        self.assertIsNotNone(st["last_run"]["ran_at"])


class TheTick(unittest.TestCase):
    def test_the_step_runs_after_corroborate_and_before_the_findings(self):
        import tick_reader
        calls = [name for _line, _fn, name, _words in tick_reader.tick_calls(ROOT)]
        self.assertIn("review-policy", calls)
        self.assertLess(calls.index("corroborate"), calls.index("review-policy"))
        self.assertLess(calls.index("review-policy"), calls.index("findings"))

    def test_it_is_an_optional_tail_step_with_a_small_limit(self):
        import tick_budget
        sys.path.insert(0, str(ROOT / "tools"))
        import tick_lease
        self.assertIn("review-policy", tick_lease.TAIL_STEPS)
        self.assertNotIn("review-policy", tick_lease.CORE_TAIL_STEPS)
        self.assertNotIn("review-policy", tick_budget.MUST_FINISH)
        self.assertLessEqual(tick_lease.STEP_SECONDS["review-policy"], 120)



def _token() -> str:
    """A credential-shaped string built at run time, so no literal token sits in the source."""
    return "gh" + "p_" + "aB3dE5gH7jK9mN1pQ3sT5vX7z"


def _url_with_password() -> str:
    return "https" + "://" + "someone" + ":" + "hunter2x" + "@" + "db.example.invalid/x"


def _batch_file(name: str) -> pathlib.Path:
    out_file = BASE / name
    code, out = cli("batch", "prepare", "--out", str(out_file))
    assert code == 0, out
    return out_file


class BatchOnlyDecidesPending(Case):
    """Review finding 1: `batch apply` decided items that were not pending."""

    def forged(self, queue: str, item: str, decision: str) -> dict:
        conn = connect()
        try:
            row = RP.current_row(conn, queue, item)
        finally:
            conn.close()
        return {"id": item, "queue": queue, "kind": "", "target": "", "created_at": "", "summary": "",
                "recommended": decision, "decision": decision, "reason": "forged by an agent",
                "basis": "heuristic:a-person-reads", "evidence": [], "hash": RP.item_hash(queue, row)}

    def test_an_item_already_decided_is_not_decided_again(self):
        pid = propose({"description": "The tool"}, target=TOOL)
        conn = connect()
        with conn:
            conn.execute("UPDATE proposals SET status='accepted', decided_by='operator' WHERE id=?", (pid,))
        conn.close()
        conclusion("mem:seen")
        conn = connect()
        L.transition(conn, "mem:seen", to_state="observed", owner=L.OPERATOR, expected_revision=1, why="seen")
        conn.close()
        f = BASE / "forged.json"
        f.write_text(json.dumps({"schema": "review-batch/1", "items": [
            self.forged("registry", pid, "reject"), self.forged("ledger", "mem:seen", "reject")]}))
        code, out = cli("batch", "apply", str(f), terminal=True, answer="y\n")
        self.assertEqual(proposal(pid)["status"], "accepted", out)
        conn = connect()
        self.assertEqual(L.current(conn, "mem:seen")["state"], "observed", out)
        conn.close()
        self.assertIn("not pending", out)

    def test_an_item_the_engine_did_not_prepare_is_refused(self):
        enable()
        pid = propose({"resources": [{"kind": "server", "identifier": "f.example.invalid"}]})
        f = BASE / "unprepared.json"
        f.write_text(json.dumps({"schema": "review-batch/1",
                                 "items": [self.forged("registry", pid, "reject")]}))
        code, out = cli("batch", "apply", str(f), terminal=True, answer="y\n")
        self.assertEqual(proposal(pid)["status"], "proposed", out)
        self.assertEqual(proposal(pid)["decided_by"], None)
        self.assertIn("not prepared", out)
        self.assertIn(pid, out)

    def test_accepting_or_rejecting_a_decided_row_is_refused_and_writes_nothing(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import review
        pid = propose({"resources": [{"kind": "server", "identifier": "g.example.invalid"}]})
        conn = connect()
        with conn:
            conn.execute("UPDATE proposals SET status='rejected', decided_by='operator' WHERE id=?", (pid,))
        row = dict(conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone())
        row["status"] = "proposed"            # what a stale caller believes
        before = (HOME / "config/project_overrides.json").read_bytes()
        with self.assertRaises(review.Refused):
            review.accept_proposal(conn, row, "stale")
        with self.assertRaises(review.Refused):
            review.reject_proposal(conn, row, "stale")
        conn.close()
        self.assertEqual((HOME / "config/project_overrides.json").read_bytes(), before)
        self.assertEqual(proposal(pid)["status"], "rejected")


class BatchShowsWhatChanged(Case):
    """Review finding 2: the person approves edits the launch never showed."""

    def test_every_departure_from_the_engine_is_listed_before_the_question(self):
        ids = [propose({"organization": "external"}, target=TOOL, created=stamp(20 - n)) for n in range(10)]
        f = _batch_file("edited.json")
        doc = json.loads(f.read_text())
        by = {it["id"]: it for it in doc["items"]}
        last = ids[-1]
        # The edit hides itself: `recommended` is changed with `decision`.
        by[last]["decision"] = by[last]["recommended"] = "accept"
        by[ids[1]]["reason"] = "an agent rewrote this reason"
        doc["items"].append({**by[ids[1]], "id": "prop:not-in-the-queue"})
        f.write_text(json.dumps(doc))
        code, out = cli("batch", "apply", str(f), terminal=True, answer="n\n")
        self.assertNotEqual(code, 0)
        prompt = out.index("Apply these")
        self.assertIn(last, out[:prompt])
        self.assertIn("differs from the engine", out[:prompt])
        self.assertIn(f"{ids[1]}: reason changed", out[:prompt])
        self.assertIn("prop:not-in-the-queue", out[:prompt])
        self.assertEqual([proposal(i)["status"] for i in ids], ["proposed"] * 10)


class ResourceFieldsAreChecked(Case):
    """Review finding 3: fields of a resource reached the curation file unchecked."""

    def setUp(self):
        super().setUp()
        enable()

    def test_added_by_another_name_than_the_proposer_waits_for_a_person(self):
        forged = propose({"resources": [{"kind": "server", "identifier": "h.example.invalid",
                                         "added_by": "operator"}]})
        own = propose({"resources": [{"kind": "server", "identifier": "i.example.invalid",
                                      "added_by": AGENT}]})
        receipt = run_policy()
        self.assertEqual(proposal(forged)["status"], "proposed")
        self.assertEqual(proposal(own)["status"], "accepted")
        res = read("config/project_overrides.json")["projects"][APP]["resources"]
        self.assertNotIn("operator", json.dumps(res))
        conn = connect()
        try:
            plan = RP.evaluate(conn, RP.load().rules_on())
        finally:
            conn.close()
        self.assertIn("added_by", " ".join(w.reason for w in plan.waiting if w.item == forged))
        self.assertEqual(receipt["applied"], {"registry.resources-append": 1})

    def test_a_credential_shape_in_any_field_waits_for_a_person(self):
        shaped = [propose({"resources": [{"kind": "server", "identifier": "j.example.invalid", "note": _token()}]}),
                  propose({"resources": [{"kind": "database", "identifier": "db-1", "url": _url_with_password()}]}),
                  propose({"resources": [{"kind": "cloud-account", "identifier": "acct-1", "account": _token()}]}),
                  propose({"aliases": [{"id": "store:example-7", "owner": "agent:store", "source": _token()}]})]
        clean = propose({"resources": [{"kind": "server", "identifier": "k.example.invalid", "note": "a web host"}]})
        run_policy()
        self.assertEqual([proposal(i)["status"] for i in shaped], ["proposed"] * 4)
        self.assertEqual(proposal(clean)["status"], "accepted")
        text = (HOME / "config/project_overrides.json").read_text() + (HOME / "config/identifiers.json").read_text()
        self.assertNotIn(_token(), text)
        self.assertNotIn("hunter2x", text)
        conn = connect()
        try:
            plan = RP.evaluate(conn, RP.load().rules_on())
        finally:
            conn.close()
        reasons = {w.item: w.reason for w in plan.waiting}
        for i in shaped:
            self.assertIn("credential", reasons[i])
            self.assertNotIn(_token(), reasons[i])


class Crash(Case):
    """Review finding 4: a crash between the steps left changes no undo could find."""

    def setUp(self):
        super().setUp()
        enable()

    def crash_on_write_of(self, name: str):
        import atomic
        real = atomic.write_json

        def write_then_die(path, doc, *a, **kw):
            real(path, doc, *a, **kw)
            if pathlib.Path(path).name == name:
                raise RuntimeError("killed after the file write")
        return unittest.mock.patch.object(atomic, "write_json", write_then_die)

    def test_a_kill_after_the_file_write_is_reverted_and_decided_again(self):
        pid = propose({"resources": [{"kind": "server", "identifier": "m.example.invalid"}]})
        with self.crash_on_write_of("project_overrides.json"), self.assertRaises(RuntimeError):
            run_policy()
        self.assertEqual(proposal(pid)["status"], "proposed")
        self.assertEqual(RP.active_decisions(), [], "a pending intent is not a decision in force")
        self.assertEqual(len(RP.pending_decisions()), 1)
        receipt = run_policy()
        self.assertEqual(receipt["recovered"], 1)
        self.assertEqual(RP.pending_decisions(), [])
        self.assertEqual(proposal(pid)["status"], "accepted")
        res = read("config/project_overrides.json")["projects"][APP]["resources"]
        self.assertEqual([r["identifier"] for r in res].count("m.example.invalid"), 1)
        self.assertEqual(len(RP.active_decisions()), 1)
        code, out = cli("undo-policy", RP.active_decisions()[0]["id"], terminal=True)
        self.assertEqual(code, 0, out)
        res = read("config/project_overrides.json")["projects"][APP]["resources"]
        self.assertNotIn("m.example.invalid", [r["identifier"] for r in res])
        self.assertEqual(proposal(pid)["status"], "proposed")

    def test_a_kill_after_the_statuses_is_reverted_by_the_next_writer(self):
        pid = propose({"resources": [{"kind": "server", "identifier": "o.example.invalid"}]}, target=TOOL)
        with unittest.mock.patch.object(RP, "_close_intent", side_effect=RuntimeError("killed")), \
                self.assertRaises(RuntimeError):
            run_policy()
        self.assertEqual(proposal(pid)["status"], "accepted")
        self.assertEqual(RP.active_decisions(), [])
        # The next writer is a person's undo: it settles the intent first, and nothing is left behind.
        code, out = cli("undo-policy", "--since", "2000-01-01T00:00:00Z", terminal=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(proposal(pid)["status"], "proposed")
        self.assertNotIn(TOOL, read("config/project_overrides.json")["projects"])
        self.assertEqual(RP.pending_decisions(), [])
        self.assertEqual(run_policy()["applied"], {"registry.resources-append": 1})
        self.assertEqual(proposal(pid)["status"], "accepted")



class CrashThenAPerson(Case):
    """Round-2 finding: a person's decision after a crash, and the recovery that follows it."""

    def setUp(self):
        super().setUp()
        enable()

    crash_on_write_of = Crash.crash_on_write_of

    def crashed(self, identifier: str, target: str = APP) -> str:
        pid = propose({"resources": [{"kind": "server", "identifier": identifier}]}, target=target)
        with self.crash_on_write_of("project_overrides.json"), self.assertRaises(RuntimeError):
            run_policy()
        self.assertEqual(len(RP.pending_decisions()), 1)
        return pid

    def identifiers(self, target: str = APP) -> list[str]:
        row = read("config/project_overrides.json")["projects"].get(target) or {}
        return [r["identifier"] for r in row.get("resources") or []]

    def test_reject_proposal_after_a_crash_removes_what_the_run_wrote(self):
        pid = self.crashed("r.example.invalid")
        code, out = cli("reject-proposal", pid, "--why", "not ours", terminal=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(RP.pending_decisions(), [], "the person's command settles the intent first")
        self.assertEqual(proposal(pid)["status"], "rejected")
        self.assertNotIn("r.example.invalid", self.identifiers())
        run_policy()
        self.assertNotIn("r.example.invalid", self.identifiers())
        self.assertEqual(proposal(pid)["status"], "rejected")

    def test_accept_proposal_after_a_crash_keeps_the_resource_once_as_the_persons(self):
        pid = self.crashed("s.example.invalid")
        code, out = cli("accept-proposal", pid, "--why", "ours", terminal=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(RP.pending_decisions(), [])
        self.assertEqual((proposal(pid)["status"], proposal(pid)["decided_by"]), ("accepted", "operator"))
        self.assertEqual(self.identifiers().count("s.example.invalid"), 1)
        run_policy()
        self.assertEqual(self.identifiers().count("s.example.invalid"), 1)

    def settle_after(self, status: str, identifier: str) -> tuple[str, dict]:
        pid = self.crashed(identifier)
        # A writer that does not recover (an older engine, a hand edit of the store) decides it.
        conn = connect()
        with conn:
            conn.execute("UPDATE proposals SET status = ?, decided_by = 'operator' WHERE id = ?", (status, pid))
        conn.close()
        receipt = run_policy()
        return pid, receipt

    def test_recovery_after_a_persons_rejection_reverts_the_files_and_says_so(self):
        pid, receipt = self.settle_after("rejected", "t.example.invalid")
        self.assertNotIn("t.example.invalid", self.identifiers())
        self.assertEqual(proposal(pid)["status"], "rejected")
        self.assertEqual(receipt["recovery"]["reverted"], 1)
        self.assertEqual(RP.pending_decisions(), [])
        rows = [json.loads(line) for line in RP.journal_path().read_text().splitlines()]
        self.assertTrue(any(r.get("recovered") and "rejected" in r.get("result", "") for r in rows))

    def test_recovery_after_a_persons_acceptance_keeps_the_files_and_leaves_the_intent_settled(self):
        pid, receipt = self.settle_after("accepted", "u.example.invalid")
        self.assertIn("u.example.invalid", self.identifiers())
        self.assertEqual(receipt["recovery"], {"reverted": 0, "left": 1, "open": 0, "degraded": []})
        rows = [json.loads(line) for line in RP.journal_path().read_text().splitlines()]
        closed = [r for r in rows if r.get("intent_closed")]
        self.assertEqual(closed[-1]["result"], "settled", "not every decision was reverted")
        self.assertTrue(any(r.get("recovered") and "accepted" in r.get("result", "") for r in rows))

    def test_a_curation_file_broken_after_a_crash_degrades_the_run_and_keeps_the_intent(self):
        pid = self.crashed("v.example.invalid")
        good = (HOME / "config/project_overrides.json").read_text()
        (HOME / "config/project_overrides.json").write_text("{ broken by hand")
        receipt = run_policy()
        self.assertTrue(any("could not be reverted" in d for d in receipt["degraded"]), receipt["degraded"])
        self.assertEqual(receipt["applied"], {})
        self.assertEqual(len(RP.pending_decisions()), 1)
        (HOME / "config/project_overrides.json").write_text(good)
        receipt = run_policy()
        self.assertEqual(receipt["recovery"]["reverted"], 1)
        self.assertEqual(proposal(pid)["status"], "accepted")
        self.assertEqual(self.identifiers().count("v.example.invalid"), 1)


class RevertOrder(Case):
    """Round-2 low 1: decisions are taken back in reverse order of application."""

    def setUp(self):
        super().setUp()
        enable()

    def test_two_decisions_on_one_new_row_leave_no_orphan_row_on_undo(self):
        for n in range(4):
            reset()
            enable()
            propose({"resources": [{"kind": "server", "identifier": "w1.example.invalid"}]}, target=TOOL,
                    created=stamp(2))
            propose({"resources": [{"kind": "server", "identifier": "w2.example.invalid"}]}, target=TOOL,
                    created=stamp(1))
            run_policy()
            conn = connect()
            try:
                with RP.lock(wait=True):
                    RP.undo(conn, RP.active_decisions())
            finally:
                conn.close()
            self.assertNotIn(TOOL, read("config/project_overrides.json")["projects"], f"round {n}")

    def test_two_decisions_on_one_new_row_leave_no_orphan_row_on_recovery(self):
        for n in range(4):
            reset()
            enable()
            propose({"resources": [{"kind": "server", "identifier": "x1.example.invalid"}]}, target=TOOL,
                    created=stamp(2))
            propose({"resources": [{"kind": "server", "identifier": "x2.example.invalid"}]}, target=TOOL,
                    created=stamp(1))
            with Crash.crash_on_write_of(self, "project_overrides.json"), self.assertRaises(RuntimeError):
                run_policy()
            conn = connect()
            try:
                with RP.lock(wait=True):
                    self.assertEqual(RP.recover(conn)["reverted"], 2)
            finally:
                conn.close()
            self.assertNotIn(TOOL, read("config/project_overrides.json")["projects"], f"round {n}")


class ReasonsEchoNoCredential(unittest.TestCase):
    """Round-2 low 4: a credential-shaped key is not quoted back in a reason."""

    def test_the_path_of_a_shaped_key_is_redacted(self):
        reason = RP._credential_problem({"resources": [{"kind": "server", _token(): "x"}]})
        self.assertIn("credential", reason)
        self.assertNotIn(_token(), reason)

    def test_no_reason_left_for_a_person_quotes_a_shaped_key(self):
        reset()
        pid = propose({"resources": [{"kind": "server", "identifier": "y.example.invalid", _token(): "x"}]})
        conn = connect()
        try:
            plan = RP.evaluate(conn, set())
            doc = RP.prepare_batch(conn)
        finally:
            conn.close()
        self.assertNotIn(_token(), json.dumps([w.reason for w in plan.waiting]))
        self.assertNotIn(_token(), json.dumps(doc))
        self.assertIn(pid, {it["id"] for it in doc["items"]})


if __name__ == "__main__":
    unittest.main()
