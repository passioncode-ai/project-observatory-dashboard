#!/usr/bin/env python3
"""embedding-policy/1 enforced: text leaves only under a remote verdict (PB-137 N-003).

The decision (N-002) lives in `embedding_policy.py` and its accepted cases. This suite
proves the three places that could send text obey it:

* `providers.embed` refuses without a remote verdict for the configured model, BEFORE
  the budget is asked, the key is read or a request is built;
* the indexer sends only eligible current records, one project per request, consumes
  what the policy keeps local, and keeps queued only what an authorized call failed;
* the query path never embeds a caller's query, and serves no vector of a project
  without a consent.

Synthetic stores in a temporary workspace; the provider is a stub that records what
it was handed. Nothing here reaches a network.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "tests"))

_CACHED = ("survey", "providers", "store.indexer", "store.ledger", "store.db", "store", "paths",
           "embedding_policy", "configuration", "textkeys")


def fresh_workspace():
    """A store and a config directory of its own; every path-caching module reloaded."""
    # Resolved: on macOS the temp directory sits behind the /var symlink, and the
    # engine's atomic writer refuses a path through a symbolic link.
    home = pathlib.Path(tempfile.mkdtemp(prefix="observatory-embed-")).resolve()
    os.environ["OBSERVATORY_HOME"] = str(home)
    os.environ["OBSERVATORY_DB"] = str(home / "store" / "observatory.db")
    # The sandbox may pin OBSERVATORY_STATE; the applied-revision record lives
    # there, and a test must not inherit the previous test's revision.
    os.environ["OBSERVATORY_STATE"] = str(home / "store")
    (home / "store").mkdir(parents=True, exist_ok=True)
    for m in _CACHED:
        sys.modules.pop(m, None)
    import paths
    paths.CONFIG.mkdir(parents=True, exist_ok=True)
    (paths.CONFIG / "models.json").write_text(
        (ROOT / "defaults/models.json").read_text(encoding="utf-8"), encoding="utf-8")
    from store import db as sdb
    from store import ledger as L
    from store import indexer as ix
    import embedding_policy as EP
    import providers
    return home, sdb.connect(), L, ix, EP, providers


def write_policy(EP, projects: dict, revision: int) -> None:
    model = EP.configured_model()
    doc = {"schema": EP.SCHEMA, "policyRevision": revision, "projects": {}}
    for project, spec in projects.items():
        doc["projects"][project] = {"remote": {
            "provider": spec.get("provider", model["provider"]),
            "model": spec.get("model", model["model"]),
            "classes": spec.get("classes", ["public", "project-internal"]),
            "consent": {"statement": f"Texts of {project} are sent to OpenAI for embeddings.",
                        "by": "operator", "at": "2026-10-04T10:00:00Z", "via": "terminal"},
            "revokedAt": spec.get("revokedAt")}}
    EP.policy_path().write_text(json.dumps(doc), encoding="utf-8")


class Recorder:
    """A provider stub: records each request (its texts and its verdict)."""

    def __init__(self, dims: int, fail: bool = False):
        self.calls: list[tuple[list[str], object]] = []
        self.dims, self.fail = dims, fail

    def __call__(self, texts, log=print, *, authorization=None):
        import providers
        if authorization is None or not authorization.remote:
            raise providers.PolicyRefused("stub: no authorization")
        self.calls.append((list(texts), authorization))
        if self.fail:
            raise providers.Retryable("the provider dropped the connection")
        return {"vectors": [[0.01 * (k + 1)] * self.dims for k, _ in enumerate(texts)],
                "tokens": len(texts), "cost": 0.0, "model": "stub", "cost_is_estimate": True}


def note(L, conn, statement, project="project:alpha", **kw):
    return L.append(conn, owner="agent:observer", statement=statement, state="proposed",
                    confidence=0.5, project_id=project, **kw)


def run_index(ix, conn):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = ix.cmd_index(conn, limit=500)
    return rc, out.getvalue(), err.getvalue()


def queued(conn) -> int:
    return conn.execute("SELECT count(*) FROM outbox WHERE consumed_at IS NULL").fetchone()[0]


def vectors(conn) -> set:
    return {r[0] for r in conn.execute("SELECT memory_id FROM vec_notes")}


class TheDoor(unittest.TestCase):
    """providers.embed itself: the one function that sends text."""

    def setUp(self):
        _h, self.conn, _L, _ix, self.EP, self.providers = fresh_workspace()
        import configuration
        self.calls: list[str] = []
        self._saved = (configuration.enabled, self.providers.check_budget,
                       self.providers.read_embed_key)
        configuration.enabled = lambda *_a, **_k: True
        self.providers.check_budget = lambda *_a, **_k: self.calls.append("budget") or "stop here"
        self.providers.read_embed_key = lambda: self.calls.append("key") or (None, "none")

    def tearDown(self):
        import configuration
        (configuration.enabled, self.providers.check_budget,
         self.providers.read_embed_key) = self._saved
        self.conn.close()

    def test_no_verdict_is_refused_before_budget_and_key(self):
        with self.assertRaises(self.providers.PolicyRefused):
            self.providers.embed(["a private thought"])
        self.assertEqual(self.calls, [])

    def test_a_local_verdict_is_refused_before_budget_and_key(self):
        local = self.EP.Verdict("local", "no-consent", 1)
        with self.assertRaises(self.providers.PolicyRefused):
            self.providers.embed(["text"], authorization=local)
        self.assertEqual(self.calls, [])

    def test_a_verdict_for_another_model_is_refused(self):
        other = self.EP.Verdict("remote", "remote-consented", 1, provider="openai",
                                model="text-embedding-3-large")
        with self.assertRaises(self.providers.PolicyRefused):
            self.providers.embed(["text"], authorization=other)
        self.assertEqual(self.calls, [])

    def test_a_matching_remote_verdict_reaches_the_budget_check_first(self):
        model = self.EP.configured_model()
        ok = self.EP.Verdict("remote", "remote-consented", 1, provider=model["provider"],
                             model=model["model"])
        with self.assertRaises(self.providers.BudgetExceeded):
            self.providers.embed(["text"], authorization=ok)
        self.assertEqual(self.calls, ["budget"], "the budget is asked, then it stops: no key read")

    def test_policy_refused_is_not_a_retryable_provider_error(self):
        self.assertFalse(issubclass(self.providers.PolicyRefused, self.providers.ProviderError))


class TheIndexer(unittest.TestCase):
    def setUp(self):
        self.home, self.conn, self.L, self.ix, self.EP, self.providers = fresh_workspace()
        if not self.ix.load_vec(self.conn):
            self.skipTest("sqlite-vec is not loadable in this interpreter")
        self.rec = Recorder(self.providers.config()["embedding"]["dims"])
        self._real = self.ix.providers.embed
        self.ix.providers.embed = self.rec

    def tearDown(self):
        self.ix.providers.embed = self._real
        self.conn.close()

    def test_no_policy_sends_nothing_and_drains_the_queue(self):
        a = note(self.L, self.conn, "a measured note")
        rc, _out, _err = run_index(self.ix, self.conn)
        self.assertEqual(rc, 0)
        self.assertEqual(self.rec.calls, [], "no consent, no request")
        self.assertEqual(queued(self.conn), 0, "a policy exclusion is permanent, not a retry")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM search_notes WHERE memory_id = ?",
                                           (a["memoryId"],)).fetchone()[0], 1)
        self.assertEqual(vectors(self.conn), set())

    def test_only_eligible_records_leave_one_project_per_request(self):
        write_policy(self.EP, {"project:alpha": {}, "project:beta": {"classes": ["public"]}}, 1)
        sent_a = note(self.L, self.conn, "alpha internal")
        public_b = note(self.L, self.conn, "beta public", project="project:beta",
                        classification="public")
        note(self.L, self.conn, "beta internal", project="project:beta")
        note(self.L, self.conn, "alpha confidential", classification="confidential")
        note(self.L, self.conn, "gamma without consent", project="project:gamma")
        note(self.L, self.conn, "alpha global", scope="global")
        note(self.L, self.conn, "no project at all", project=None)
        run_index(self.ix, self.conn)
        sent = sorted(t for texts, _v in self.rec.calls for t in texts)
        self.assertEqual(sent, ["alpha internal", "beta public"])
        for texts, verdict in self.rec.calls:
            self.assertEqual(len(texts), 1, "each request carries one project's texts")
            self.assertEqual(verdict.reason, "remote-consented")
        self.assertEqual(vectors(self.conn), {sent_a["memoryId"], public_b["memoryId"]})
        self.assertEqual(queued(self.conn), 0)

    def test_a_revoked_consent_sends_nothing(self):
        write_policy(self.EP, {"project:alpha": {"revokedAt": "2026-10-04T11:00:00Z"}}, 1)
        note(self.L, self.conn, "alpha after revocation")
        run_index(self.ix, self.conn)
        self.assertEqual(self.rec.calls, [])

    def test_only_the_current_revision_of_a_record_leaves(self):
        write_policy(self.EP, {"project:alpha": {}}, 1)
        first = note(self.L, self.conn, "the first wording")
        self.L.append(self.conn, memory_id=first["memoryId"], expected_revision=1,
                      owner="agent:observer", statement="the corrected wording",
                      state="proposed", confidence=0.6, project_id="project:alpha")
        run_index(self.ix, self.conn)
        sent = [t for texts, _v in self.rec.calls for t in texts]
        self.assertEqual(sent, ["the corrected wording"])

    def test_a_broken_policy_sends_nothing_and_says_so(self):
        self.EP.policy_path().write_text("{broken", encoding="utf-8")
        note(self.L, self.conn, "alpha note")
        _rc, _out, err = run_index(self.ix, self.conn)
        self.assertEqual(self.rec.calls, [])
        self.assertIn("embedding policy refused", err)

    def test_an_older_policy_revision_is_refused(self):
        write_policy(self.EP, {"project:alpha": {}}, 3)
        self.EP.current()
        write_policy(self.EP, {"project:alpha": {}}, 2)
        note(self.L, self.conn, "alpha note")
        _rc, _out, err = run_index(self.ix, self.conn)
        self.assertEqual(self.rec.calls, [], "a rolled-back file cannot re-open export")
        self.assertIn("refusing a rollback", err)

    def test_an_authorized_call_that_fails_keeps_its_rows_queued(self):
        write_policy(self.EP, {"project:alpha": {}}, 1)
        self.rec.fail = True
        note(self.L, self.conn, "alpha note")
        note(self.L, self.conn, "kept local", project="project:gamma")
        run_index(self.ix, self.conn)
        self.assertEqual(queued(self.conn), 1, "only the authorized row waits for a retry")


class TheQueryPath(unittest.TestCase):
    def setUp(self):
        self.home, self.conn, self.L, self.ix, self.EP, self.providers = fresh_workspace()
        if not self.ix.load_vec(self.conn):
            self.skipTest("sqlite-vec is not loadable in this interpreter")
        import survey
        self.survey = survey
        self.dims = self.providers.config()["embedding"]["dims"]
        self.asked: list[str] = []
        self._saved = (self.providers.embed, self.providers.check_budget)
        self.providers.check_budget = lambda *_a, **_k: self.asked.append("budget") or None
        rec = Recorder(self.dims)

        def embed(texts, log=print, *, authorization=None):
            self.asked.append("embed")
            return rec(texts, log, authorization=authorization)
        self.providers.embed = embed
        self.ix.providers.embed = embed

    def tearDown(self):
        self.providers.embed, self.providers.check_budget = self._saved
        self.conn.close()

    def _index(self, *notes):
        for statement, project in notes:
            note(self.L, self.conn, statement, project=project)
        run_index(self.ix, self.conn)
        self.asked.clear()

    def test_a_callers_query_is_never_embedded(self):
        write_policy(self.EP, {"project:alpha": {}}, 1)
        self._index(("alpha paywall migration", "project:alpha"))
        got = self.survey.search("paywall migration", project_id="project:alpha")
        self.assertEqual(self.asked, [], "no budget check, no embedding for a caller argument")
        reasons = " ".join(d.get("reason", "") for d in got.get("degraded", []))
        self.assertIn("untrusted-authority", reasons)
        self.assertTrue(got.get("results") or got.get("hits") is not None)

    def test_a_trusted_query_embeds_and_serves_only_consented_vectors(self):
        write_policy(self.EP, {"project:alpha": {}}, 1)
        self._index(("alpha paywall migration", "project:alpha"))
        # A vector left from before the policy, for a record the policy now keeps
        # local: confidential text in the consented project itself.
        from sqlite_vec import serialize_float32
        legacy = note(self.L, self.conn, "legacy confidential paywall vector",
                      classification="confidential")
        with self.conn:
            self.conn.execute("INSERT INTO vec_notes (memory_id, revision, embedding) VALUES (?,?,?)",
                              (legacy["memoryId"], 1, serialize_float32([0.01] * self.dims)))
        got = self.survey.search("paywall", project_id="project:alpha", authority="binding",
                                 classification="project-internal")
        self.assertEqual(self.asked, ["budget", "embed"])
        by_vector = {r["memoryId"] for r in got["results"] if "vector" in r.get("matched", [])}
        self.assertNotIn(legacy["memoryId"], by_vector,
                         "a vector the policy no longer allows is not served (the lexical "
                         "index still finds the record on this machine)")
        self.assertTrue(by_vector, "the consented record is found by its vector")

    def test_a_query_over_every_project_stays_local(self):
        write_policy(self.EP, {"project:alpha": {}}, 1)
        self._index(("alpha paywall migration", "project:alpha"))
        self.survey.search("paywall", project_id=None, authority="binding",
                           classification="public")
        self.assertEqual(self.asked, [])


class TheOperatorsCommand(unittest.TestCase):
    """`full embedding-policy`: the only writer, and only at a terminal."""

    def setUp(self):
        self.home, self.conn, *_rest, self.EP, _p = fresh_workspace()
        sys.modules.pop("embedding_policy_cli", None)
        sys.path.insert(0, str(ROOT / "tools"))
        import embedding_policy_cli
        self.cli = embedding_policy_cli

    def tearDown(self):
        self.conn.close()

    def _as_terminal(self, yes: bool):
        self.cli._is_terminal = lambda: yes

    def test_grant_and_revoke_refuse_without_a_terminal(self):
        self._as_terminal(False)
        for argv in (["grant", "project:alpha", "--statement", "Texts go to OpenAI."],
                     ["revoke", "project:alpha"]):
            with self.subTest(argv[0]):
                with self.assertRaises(SystemExit) as cm:
                    self.cli.main(argv)
                self.assertIn("without a terminal", str(cm.exception))
        self.assertFalse(self.EP.policy_path().exists(), "nothing was written")

    def test_grant_then_revoke_at_a_terminal(self):
        self._as_terminal(True)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.cli.main(["grant", "project:alpha", "--statement",
                                            "Texts of alpha are sent to OpenAI for embeddings."]), 0)
        policy = self.EP.current()
        self.assertEqual(policy.revision, 1)
        self.assertIn("project:alpha", policy.projects)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.cli.main(["revoke", "project:alpha"]), 0)
        policy = self.EP.current()
        self.assertEqual(policy.revision, 2)
        self.assertIsNotNone(policy.projects["project:alpha"].revoked_at)

    def test_a_statement_that_does_not_name_the_provider_is_refused(self):
        self._as_terminal(True)
        with self.assertRaises(SystemExit) as cm:
            self.cli.main(["grant", "project:alpha", "--statement", "ok"])
        self.assertIn("must name the provider", str(cm.exception))

    def test_confidential_cannot_be_granted(self):
        self._as_terminal(True)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.cli.main(["grant", "project:alpha", "--statement", "Texts go to OpenAI.",
                           "--class", "confidential"])


class WhatAgentsAndPeopleAreTold(unittest.TestCase):
    """Transparency is part of the contract: the texts an agent and a person read say
    what the code does, and a test fails when they drift apart."""

    def test_doctor_reports_the_policy_state(self):
        home, conn, *_r, EP, _p = fresh_workspace()
        conn.close()
        cfg, state = home / "config", home / "store"
        configured = EP.configured_model()
        st = EP.status(cfg, state, configured)
        self.assertEqual((st["state"], st["inForce"]), ("local-only", []))
        self.assertIn("nothing leaves", st["summary"])
        self.assertEqual(st["command"], "project-observatory full embedding-policy show")
        write_policy(EP, {"project:alpha": {}, "project:gone": {"revokedAt": "2026-10-04T11:00:00Z"},
                          "project:other": {"model": "text-embedding-3-large"}}, 2)
        st = EP.status(cfg, state, configured)
        self.assertEqual(st["state"], "remote-for-consented")
        self.assertEqual((st["inForce"], st["revoked"], st["otherModel"]),
                         (["project:alpha"], ["project:gone"], ["project:other"]))
        self.assertIn("agents' queries never leave", st["summary"])
        (state / EP.SEEN_FILE).write_text(json.dumps({"policyRevision": 5}), encoding="utf-8")
        st = EP.status(cfg, state, configured)
        self.assertEqual(st["state"], "refused")
        self.assertIn("rollback", st["reason"])
        EP.policy_path().write_text("{broken", encoding="utf-8")
        self.assertEqual(EP.status(cfg, state, configured)["state"], "refused")
        before = (state / EP.SEEN_FILE).read_text(encoding="utf-8")
        EP.status(cfg, state, configured)
        self.assertEqual((state / EP.SEEN_FILE).read_text(encoding="utf-8"), before,
                         "a look never records a revision")

    def test_the_mcp_instructions_say_the_query_stays_local(self):
        import ast
        tree = ast.parse((ROOT / "mcp/server.py").read_text(encoding="utf-8"))
        text = next(ast.literal_eval(n.value) for n in ast.walk(tree)
                    if isinstance(n, ast.keyword) and n.arg == "instructions")
        self.assertNotIn("embeds the query", text)
        self.assertIn("embedding-policy/1", text)
        self.assertLessEqual(len(text), 1800, "a host keeps about 2,048 characters")
        tool = (ROOT / "mcp/server.py").read_text(encoding="utf-8")
        start = tool.index("def observatory_search(")
        self.assertIn("never leaves this machine", tool[start:start + 2500])


if __name__ == "__main__":
    unittest.main()
