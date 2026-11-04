#!/usr/bin/env python3
"""`derivations.py` (OBS-321): one repository derived from another, and how far a copy is behind.

Every repository here is a temporary git repository built by the test; nothing reaches a
network or the operator's estate. The curated file, the copy's `.copy/upstream.json`, the
findings, the two readers (`project.detail`, `project.resolve`) and `full copies plan` are
each driven from planted state.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
import tmp as tmpdir                                                # noqa: E402
from git_env import git_env                                         # noqa: E402
import derivations                                                  # noqa: E402
import derivation_findings                                          # noqa: E402

COPY, SOURCE, OTHER = ("repository:example-org/alpha-agent", "repository:personal/alpha",
                       "repository:example-org/beta-api")


def git(repo: pathlib.Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60,
                       env=git_env())
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def commit(repo: pathlib.Path, path: str, text: str, when: str = "2026-10-01T12:00:00Z") -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(repo, "add", path)
    r = subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t",
                        "commit", "-qm", path], capture_output=True, text=True, timeout=60,
                       env=git_env(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when))
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return git(repo, "rev-parse", "HEAD")


def new_repo(prefix: str) -> pathlib.Path:
    repo = pathlib.Path(tmpdir.mkdtemp(prefix=f"observatory-derivations-{prefix}-"))
    git(repo, "init", "-q", "-b", "main")
    return repo


def config(entries: list, schema: str = derivations.SCHEMA) -> pathlib.Path:
    path = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-derivations-config-")) / "derivations.json"
    path.write_text(json.dumps({"schema": schema, "derivations": entries}), encoding="utf-8")
    return path


def repo_row(rid: str, path: pathlib.Path | None = None, *, archived: bool = False, branch: str = "main") -> dict:
    row = {"id": rid, "default_branch": branch, "archived": archived}
    if path is not None:
        row["local"] = {"path": str(path)}
    return row


class Estate:
    """A source with history, and a copy taken at its second commit."""

    def __init__(self, upstream: dict | None = None):
        self.source = new_repo("source")
        self.first = commit(self.source, "README.md", "one")
        self.second = commit(self.source, "app.py", "two")
        self.third = commit(self.source, "app.py", "three", "2026-10-02T12:00:00Z")
        self.fourth = commit(self.source, "docs/runs/note.md", "run data", "2026-10-03T12:00:00Z")
        self.copy = new_repo("copy")
        commit(self.copy, "app.py", "two")
        if upstream is not None:
            commit(self.copy, ".copy/upstream.json", json.dumps(upstream))

    def repos(self, **kw) -> list[dict]:
        return [repo_row(COPY, self.copy), repo_row(SOURCE, self.source, **kw), repo_row(OTHER)]


def entry(**kw) -> dict:
    base = {"copy": COPY, "source": SOURCE, "kind": "copy"}
    base.update(kw)
    return base


class CuratedFile(unittest.TestCase):
    REPOS = {COPY: {}, SOURCE: {}, OTHER: {}}

    def codes(self, entries: list) -> list[str]:
        _, refused, degraded = derivations.read_config(config(entries), self.REPOS)
        self.assertEqual(len(refused), len(degraded), "every refusal is named in degraded")
        for r, d in zip(refused, degraded):
            self.assertIn(f"refused ({r['code']})", d["reason"])
        return [r["code"] for r in refused]

    def test_an_absent_file_is_no_relation_and_nothing_degraded(self):
        missing = pathlib.Path(tmpdir.mkdtemp()) / "derivations.json"
        self.assertEqual(derivations.read_config(missing, self.REPOS), ([], [], []))

    def test_a_file_that_is_not_the_schema_is_refused_whole(self):
        for text in ("{", json.dumps({"derivations": []}), json.dumps([1])):
            path = pathlib.Path(tmpdir.mkdtemp()) / "derivations.json"
            path.write_text(text, encoding="utf-8")
            accepted, refused, degraded = derivations.read_config(path, self.REPOS)
            self.assertEqual((accepted, refused), ([], []))
            self.assertIn("refused (unreadable)", degraded[0]["reason"])

    def test_each_wrong_entry_is_refused_by_name_never_repaired(self):
        self.assertEqual(self.codes([entry(copy="repository:example-org/nowhere", copy_point="abc1234")]),
                         ["unknown-repository"])
        self.assertEqual(self.codes([entry(source=COPY, copy_point="abc1234")]), ["self-derivation"])
        self.assertEqual(self.codes([entry(kind="move", copy_point="abc1234", last_synced="abc1234")]),
                         ["move-with-last-synced"])
        self.assertEqual(self.codes([entry(kind="fork", copy_point="abc1234")]), ["bad-kind"])
        self.assertEqual(self.codes([entry(copy_point="abc1234", origin="x")]), ["unknown-field"])
        self.assertEqual(self.codes([entry(copy_point="not-a-commit")]), ["bad-point"])
        self.assertEqual(self.codes([entry()]), ["missing-copy-point"])
        self.assertEqual(self.codes([entry(copy="alpha", copy_point="abc1234")]), ["bad-repository"])
        self.assertEqual(self.codes([entry(copy_point="abc1234", builder={"rules_count": 3})]), ["bad-builder"])
        self.assertEqual(self.codes([entry(copy_point="abc1234", source_branch="-x")]), ["bad-branch"])
        self.assertEqual(self.codes(["x"]), ["not-an-object"])

    def test_one_copy_has_one_source_and_a_cycle_is_refused(self):
        self.assertEqual(self.codes([entry(copy_point="abc1234"), entry(source=OTHER, copy_point="abc1234")]),
                         ["duplicate-copy", "duplicate-copy"])
        codes = self.codes([entry(copy_point="abc1234"),
                            {"copy": SOURCE, "source": OTHER, "kind": "copy", "copy_point": "abc1234"},
                            {"copy": OTHER, "source": COPY, "kind": "copy", "copy_point": "abc1234"}])
        self.assertEqual(codes, ["cycle", "cycle", "cycle"])
        _, refused, _ = derivations.read_config(config([entry(copy_point="abc1234"),
                                                        {"copy": SOURCE, "source": COPY, "kind": "move"}]),
                                                self.REPOS)
        self.assertIn(f"{COPY} -> {SOURCE} -> {COPY}", refused[0]["reason"])

    def test_a_chain_and_a_move_without_copy_point_are_accepted(self):
        accepted, refused, _ = derivations.read_config(config([
            entry(copy_point={"source_commit": "ABC1234", "copy_commit": "def5678", "date": "2026-11-02"},
                  builder={"path_hint": "~/builders/alpha/build.sh", "rules_count": 4, "rules_sha256": "a" * 64}),
            {"copy": SOURCE, "source": OTHER, "kind": "move"}]), self.REPOS)
        self.assertEqual(refused, [])
        self.assertEqual(accepted[0]["copy_point"], {"source_commit": "abc1234", "copy_commit": "def5678",
                                                     "date": "2026-11-02"})
        self.assertEqual(accepted[1]["kind"], "move")


class Measured(unittest.TestCase):
    def build(self, estate: Estate, entries: list, **kw) -> dict:
        return derivations.build(config(entries), estate.repos(**kw), "2026-11-04")

    def test_behind_is_counted_in_the_source_clone_from_last_synced(self):
        estate = Estate({"kind": "copy", "copy_point": "0" * 7, "deny": ["docs/runs"]})
        doc = self.build(estate, [entry(copy_point=estate.first[:9], last_synced=estate.second[:12])])
        rel = doc["relations"][0]
        behind = rel["behind"]
        self.assertEqual(behind["state"], "measured")
        self.assertEqual(behind["commits"], 2)
        self.assertEqual(behind["outside_deny"], 1, "the run-data commit is in a deny path")
        self.assertEqual(behind["ref"], "main")
        self.assertEqual(behind["head"], estate.fourth[:12])
        self.assertTrue(behind["on_branch"])
        self.assertEqual(behind["oldest_on"], "2026-10-02")
        self.assertEqual(rel["provenance"], {"kind": "curated", "copy_point": "curated", "last_synced": "curated",
                                             "deny": "copy"})
        self.assertEqual(rel["copy_record"]["differs"],
                         [{"field": "copy_point", "curated": estate.first[:9], "copy": "0000000"}])
        self.assertEqual(doc["source_refs"], [derivations.SOURCE_ID])

    def test_the_copy_fills_what_the_curated_entry_leaves_out_and_says_so(self):
        estate = Estate()
        commit(estate.copy, ".copy/upstream.json", json.dumps({"kind": "copy", "last_synced": estate.third,
                                                               "source": "never read"}))
        doc = self.build(estate, [entry(copy_point=estate.first)])
        rel = doc["relations"][0]
        self.assertEqual(rel["provenance"]["last_synced"], "copy")
        self.assertEqual(rel["behind"]["commits"], 1)
        self.assertIsNone(rel["deny"])
        self.assertIsNone(rel["behind"]["outside_deny"])
        self.assertNotIn("never read", json.dumps(doc))

    def test_an_uncommitted_record_does_not_count(self):
        estate = Estate()
        (estate.copy / ".copy").mkdir()
        (estate.copy / ".copy" / "upstream.json").write_text(json.dumps({"kind": "copy"}), encoding="utf-8")
        doc = self.build(estate, [entry(copy_point=estate.second)])
        self.assertEqual(doc["relations"][0]["copy_record"]["state"], "absent")
        self.assertEqual(doc["unrecorded"], [])

    def test_unknown_is_said_with_its_reason_never_guessed(self):
        estate = Estate()
        missing = derivations.build(config([entry(copy_point="1234567")]), estate.repos(), "2026-11-04")
        self.assertEqual(missing["relations"][0]["behind"]["state"], "unknown")
        self.assertIn("not in the local clone", missing["relations"][0]["behind"]["reason"])
        no_clone = derivations.build(config([entry(copy_point=estate.second)]),
                                     [repo_row(COPY, estate.copy), repo_row(SOURCE)], "2026-11-04")
        self.assertIn("no local clone of the source", no_clone["relations"][0]["behind"]["reason"])
        no_branch = derivations.build(config([entry(copy_point=estate.second, source_branch="release")]),
                                      estate.repos(), "2026-11-04")
        self.assertIn("branch release", no_branch["relations"][0]["behind"]["reason"])

    def test_origin_branch_is_read_before_the_local_one_and_nothing_is_fetched(self):
        estate = Estate()
        git(estate.source, "update-ref", "refs/remotes/origin/main", estate.third)
        doc = self.build(estate, [entry(copy_point=estate.second)])
        self.assertEqual(doc["relations"][0]["behind"]["ref"], "origin/main")
        self.assertEqual(doc["relations"][0]["behind"]["commits"], 1)

    def test_a_copy_taken_from_another_branch_is_off_branch(self):
        estate = Estate()
        git(estate.source, "switch", "-q", "-c", "side", estate.first)
        side = commit(estate.source, "side.txt", "side")
        doc = self.build(estate, [entry(copy_point=side)])
        self.assertFalse(doc["relations"][0]["behind"]["on_branch"])

    def test_a_copy_that_says_so_without_an_entry_is_unrecorded(self):
        estate = Estate({"kind": "copy", "copy_point": "abc1234"})
        doc = self.build(estate, [])
        self.assertEqual([u["copy"] for u in doc["unrecorded"]], [COPY])
        refused = self.build(estate, [entry(copy_point="abc1234", kind="fork")])
        self.assertTrue(refused["unrecorded"][0]["refused"])

    def test_a_move_is_never_measured(self):
        estate = Estate()
        doc = self.build(estate, [entry(kind="move", copy_point=estate.fourth)])
        self.assertIsNone(doc["relations"][0]["behind"])
        self.assertFalse(doc["relations"][0]["source_archived"])


class Findings(unittest.TestCase):
    def rel(self, **kw) -> dict:
        base = {"copy": COPY, "source": SOURCE, "kind": "copy",
                "copy_point": {"source_commit": "1111111"}, "last_synced": {"source_commit": "2222222"},
                "copy_record": {"state": "read", "differs": []},
                "behind": {"state": "measured", "commits": 3, "outside_deny": 2, "ref": "origin/main",
                           "head": "333333333333", "on_branch": True, "oldest_on": "2026-10-01",
                           "since": "last_synced"},
                "source_archived": False}
        base.update(kw)
        return base

    def rows(self, *rels, unrecorded=(), today="2026-11-04") -> list[dict]:
        return derivation_findings.findings({"relations": list(rels), "unrecorded": list(unrecorded)}, today)

    def test_behind_is_a_warning_once_the_oldest_commit_is_a_week_old(self):
        [row] = self.rows(self.rel())
        self.assertEqual((row["type"], row["subject"], row["severity"]), ("copy.behind_source", COPY, "warning"))
        self.assertEqual(row["title_args"]["n"], 2, "commits in deny paths are not owed")
        self.assertIn(f"copies plan {COPY}", row["action"])
        [fresh] = self.rows(self.rel(), today="2026-10-05")
        self.assertEqual(fresh["severity"], "info")
        self.assertEqual(self.rows(self.rel(behind={**self.rel()["behind"], "outside_deny": 0})), [],
                         "only deny-path commits: nothing to carry")

    def test_unknown_is_its_own_row(self):
        [row] = self.rows(self.rel(behind={"state": "unknown", "commits": None, "reason": "no clone"}))
        self.assertEqual(row["severity"], "info")
        self.assertIn("not measured", row["title"])

    def test_unrecorded_both_ways_and_a_record_that_disagrees(self):
        rows = self.rows(self.rel(behind=None, copy_record={"state": "absent", "differs": []}),
                         unrecorded=[{"copy": OTHER, "kind": "copy", "refused": False}])
        self.assertEqual(sorted((r["type"], r["subject"]) for r in rows),
                         [("copy.relation_unrecorded", COPY), ("copy.relation_unrecorded", OTHER)])
        [row] = self.rows(self.rel(behind=None, copy_record={
            "state": "read", "differs": [{"field": "last_synced", "curated": "2222222", "copy": "4444444"}]}))
        self.assertEqual((row["type"], row["severity"]), ("copy.record_differs", "warning"))
        self.assertIn("4444444", row["detail"])

    def test_a_moved_source_must_be_archived(self):
        [row] = self.rows(self.rel(kind="move", behind=None, last_synced=None))
        self.assertEqual((row["type"], row["subject"]), ("copy.moved_source_open", SOURCE))
        self.assertEqual(self.rows(self.rel(kind="move", behind=None, source_archived=True)), [])

    def test_no_document_no_rows(self):
        self.assertEqual(derivation_findings.findings(None), [])
        self.assertEqual(derivation_findings.from_file(pathlib.Path(tmpdir.mkdtemp()) / "x.json"), [])


class Readers(unittest.TestCase):
    def test_both_ways_and_refusals_are_degraded_for_the_repositories_they_name(self):
        doc = {"relations": [{"copy": COPY, "source": SOURCE, "kind": "copy",
                              "copy_point": {"source_commit": "1111111"}, "last_synced": None,
                              "behind": {"state": "measured", "commits": 4}}],
               "refused": [{"copy": OTHER, "source": SOURCE, "reason": "entry 1 refused (cycle): x"}]}
        derived, copies, degraded = derivations.relations_of(doc, [COPY])
        self.assertEqual(derived, [{"copy": COPY, "source": SOURCE, "kind": "copy", "copy_point": "1111111",
                                    "last_synced": None, "behind": 4}])
        self.assertEqual(copies, [])
        self.assertEqual(degraded, [])
        derived, copies, degraded = derivations.relations_of(doc, [SOURCE])
        self.assertEqual((len(derived), len(copies)), (0, 1))
        self.assertEqual(degraded, [{"source": "config/derivations.json", "reason": "entry 1 refused (cycle): x"}])

    def test_load_says_when_a_curated_file_was_not_emitted(self):
        reg = pathlib.Path(tmpdir.mkdtemp())
        cfg = reg / "derivations-config.json"
        self.assertEqual(derivations.load(reg, cfg), (None, []))
        cfg.write_text("{}", encoding="utf-8")
        doc, degraded = derivations.load(reg, cfg)
        self.assertIsNone(doc)
        self.assertIn("full emit", degraded[0]["reason"])


class ThroughTheEngine(unittest.TestCase):
    """The emit, the readers and the CLI over a synthetic workspace (emitter_fixture)."""

    @classmethod
    def setUpClass(cls):
        import workspace
        from emitter_fixture import environment, seed
        cls.estate = Estate({"kind": "copy", "deny": ["docs/runs"]})
        root = pathlib.Path(tmpdir.mkdtemp(prefix="observatory-derivations-emit-"))
        seed(root)
        workspace.initialize(root / "home", identities=False)
        model = json.loads((root / "raw" / "model.json").read_text(encoding="utf-8"))
        base = model["repositories"]["fixture/service"]
        for nwo, path in (("example-org/alpha-agent", cls.estate.copy), ("personal/alpha", cls.estate.source)):
            model["repositories"][nwo] = {**base, "url": f"https://github.com/{nwo}",
                                          "default_branch": "main",
                                          "local": {"folder": path.name, "path": str(path), "symlink": False,
                                                    "kinds": [], "branch": "main", "commits": 2,
                                                    "last_commit": "2026-10-01", "dirty": 0}}
        model["projects"]["fixture-a"]["repos"] = ["fixture/service", "example-org/alpha-agent"]
        model["projects"]["fixture-b"]["repos"] = ["personal/alpha"]
        (root / "raw" / "model.json").write_text(json.dumps(model), encoding="utf-8")
        (root / "home" / "config" / "derivations.json").write_text(json.dumps({
            "schema": derivations.SCHEMA, "derivations": [
                entry(copy_point=cls.estate.first, last_synced=cls.estate.second,
                      builder={"path_hint": "builders/alpha/build.sh", "rules_count": 2}),
                {"copy": "repository:fixture/service", "source": "repository:fixture/service", "kind": "copy",
                 "copy_point": "abc1234"}]}), encoding="utf-8")
        cls.root = root
        cls.env = git_env({**environment(root), "OBSERVATORY_HOME": str(root / "home")})
        cls.emit = subprocess.run([sys.executable, "collectors/emit_registry.py", str(root / "raw")], cwd=ROOT,
                                  capture_output=True, text=True, timeout=300, env=cls.env)

    def py(self, code: str) -> dict:
        p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=300,
                           env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_the_emit_writes_the_relations_and_declares_its_source(self):
        self.assertEqual(self.emit.returncode, 0, self.emit.stderr[-800:])
        doc = json.loads((self.root / "registry" / "derivations.json").read_text(encoding="utf-8"))
        self.assertEqual([(r["copy"], r["behind"]["commits"], r["behind"]["outside_deny"])
                          for r in doc["relations"]], [(COPY, 2, 1)])
        self.assertEqual([r["code"] for r in doc["refused"]], ["self-derivation"])
        self.assertIn("refused (self-derivation)", doc["degraded"][0]["reason"])
        sources = json.loads((self.root / "registry" / "sources.json").read_text(encoding="utf-8"))["sources"]
        src = next(s for s in sources if s["id"] == derivations.SOURCE_ID)
        self.assertEqual(src["evidence_for"], ["registry:derivations.json"])
        self.assertEqual(src["availability"], "measured-input")

    def test_project_detail_and_resolve_show_the_relation_both_ways(self):
        import jsonschema
        detail = self.py("import json, sys; sys.path.insert(0, '.'); import survey; "
                         "print(json.dumps(survey.project_detail('project:fixture-a', timeline_limit=0)))")
        self.assertEqual([r["source"] for r in detail["project"]["derived_from"]], [SOURCE])
        self.assertEqual(detail["project"]["derived_from"][0]["behind"], 2)
        self.assertEqual(detail["project"]["copies"], [])
        self.assertTrue(any("self-derivation" in d["reason"] for d in detail["degraded"]),
                        "a refusal naming the project's repository is degraded")
        schema = json.loads((ROOT / "fabric/schemas/project-detail-output.schema.json").read_text(encoding="utf-8"))
        jsonschema.validate(detail, schema)
        resolved = self.py("import json, sys; sys.path[:0] = ['.', 'mcp']; import server; "
                           "print(json.dumps(server.observatory_resolve(id='project:fixture-b')))")
        self.assertEqual([r["copy"] for r in resolved["copies"]], [COPY])
        self.assertEqual(resolved["derived_from"], [])
        schema = json.loads((ROOT / "fabric/schemas/project-resolve-output.schema.json").read_text(encoding="utf-8"))
        jsonschema.validate(resolved, schema)

    def test_the_findings_builder_reads_the_document(self):
        rows = self.py("import json, sys; sys.path[:0] = ['.', 'tools']; import paths, derivation_findings; "
                       "print(json.dumps(derivation_findings.from_file(paths.REGISTRY / 'derivations.json', "
                       "'2026-11-04')))")
        self.assertEqual([(r["type"], r["subject"], r["severity"]) for r in rows],
                         [("copy.behind_source", COPY, "warning")])
        build = (ROOT / "tools" / "build_findings.py").read_text(encoding="utf-8")
        self.assertIn("derivation_findings.from_file(paths.REGISTRY / \"derivations.json\", TODAY)", build)

    def test_copies_plan_prints_the_steps_and_runs_nothing(self):
        before = git(self.estate.copy, "rev-parse", "HEAD"), git(self.estate.source, "rev-parse", "HEAD")
        p = subprocess.run([sys.executable, "observatory.py", "copies", "plan", COPY], cwd=ROOT,
                           capture_output=True, text=True, timeout=120, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr[-500:])
        self.assertIn(f"sync/{self.estate.fourth[:12]}", p.stdout)
        self.assertIn("git apply --3way", p.stdout)
        self.assertIn(":(exclude)docs/runs", p.stdout)
        self.assertIn("Nothing below is run", p.stdout)
        self.assertEqual((git(self.estate.copy, "rev-parse", "HEAD"), git(self.estate.source, "rev-parse", "HEAD")),
                         before)
        self.assertEqual(git(self.estate.copy, "branch", "--list", "sync/*"), "")
        p = subprocess.run([sys.executable, "observatory.py", "copies", "plan", "repository:fixture/service"],
                           cwd=ROOT, capture_output=True, text=True, timeout=120, env=self.env)
        self.assertEqual(p.returncode, 2)
        self.assertIn("self-derivation", p.stderr)
        p = subprocess.run([sys.executable, "observatory.py", "copies", "list"], cwd=ROOT,
                           capture_output=True, text=True, timeout=120, env=self.env)
        self.assertIn(f"{COPY}  <-  {SOURCE}  (2 behind)", p.stdout)


class Documented(unittest.TestCase):
    def test_every_refusal_code_and_finding_type_is_documented(self):
        docs = (ROOT.parents[1] / "docs" / "COMPATIBILITY.md")
        if not docs.is_file():
            self.skipTest("the repository's docs are not beside this engine copy")
        text = docs.read_text(encoding="utf-8")
        for code in derivations.CODES:
            self.assertIn(f"`{code}`", text)
        for kind in derivation_findings.TYPES:
            self.assertIn(f"`{kind}`", text)


if __name__ == "__main__":
    unittest.main()
